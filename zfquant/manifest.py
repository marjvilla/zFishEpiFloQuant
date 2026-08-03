"""Which plane holds which fish's which channel -- and how to correct it.

Same portability constraint as core.py: Jython 2.7 and CPython 3, no ImageJ.

The problem this solves
-----------------------
Three different data layouts all call themselves "a stack", and they need
different navigation:

  HYPERSTACK   a real channel dimension; position is (c, z, t)
  FLAT_STACK   one plain stack holding every fish and channel as slices, with
               no fixed "slice N = channel X" rule
  PER_WINDOW   one separate single-plane image per channel

The legacy tool tried to cope with all three at once at every call site, and
auto-navigating a flat stack to a static slice index was its single worst
recurring bug -- it looked like it worked, because the display updated and a
number got written, but it measured the wrong plane.

Here the layout is answered once at setup and turned into an explicit mapping
from (fish, channel) to a plane. Everything downstream reads the mapping, so
there is one place to get right and one place to test.

The mapping is a default, not a contract. Real datasets contain the odd
mislabeled or misplaced fish, so every entry can be overridden per fish, and
every override is recorded in the row rather than applied silently.
"""

from __future__ import division, print_function

import json
import os


LAYOUT_HYPERSTACK = "hyperstack"
LAYOUT_FLAT_STACK = "flat_stack"
LAYOUT_PER_WINDOW = "per_window"

LAYOUTS = (LAYOUT_HYPERSTACK, LAYOUT_FLAT_STACK, LAYOUT_PER_WINDOW)

# Plain-language consequence of each choice, shown at setup so the operator
# picks with their eyes open instead of discovering the behaviour later. Also
# used verbatim as the review-mode labels in the setup dropdown -- see
# review.py, which walks a position sequence appropriate to each.
LAYOUT_DESCRIPTIONS = {
    LAYOUT_HYPERSTACK: ("Auto Hyperstack -- one image with a real channel "
                        "dimension. You'll step through each frame (or "
                        "slice) and confirm what's there before measuring."),
    LAYOUT_FLAT_STACK: ("Auto Single Stack -- one big stack holding every "
                        "fish and channel as slices. You'll step through "
                        "each block of slices and confirm what's there "
                        "before measuring."),
    LAYOUT_PER_WINDOW: ("Manual -- you'll go through your open images and "
                        "say what each one is."),
}

# Why a plane differs from what the manifest predicted.
ORIGIN_MANIFEST = "manifest"      # used as planned
ORIGIN_OVERRIDE = "override"      # operator redirected this fish's channel
ORIGIN_RELABEL = "relabel"        # operator said this plane is a different channel


class Plane(object):
    """One addressable image plane.

    `image_key` identifies the window: an ImageJ image ID for a live session, or
    a title when reloading a manifest saved on a previous day (IDs are not
    stable across restarts).
    """

    def __init__(self, image_key, channel=1, z=1, t=1, slice_index=None,
                 image_title=None):
        self.image_key = image_key
        self.image_title = image_title
        self.channel = channel
        self.z = z
        self.t = t
        # Set for FLAT_STACK, where the address is a single linear slice rather
        # than a (c, z, t) triple.
        self.slice_index = slice_index

    def as_dict(self):
        return {"image_key": self.image_key, "image_title": self.image_title,
                "channel": self.channel, "z": self.z, "t": self.t,
                "slice_index": self.slice_index}

    @classmethod
    def from_dict(cls, data):
        return cls(data.get("image_key"), data.get("channel", 1),
                   data.get("z", 1), data.get("t", 1),
                   data.get("slice_index"), data.get("image_title"))

    def describe(self):
        """Human-readable address, for the panel's 'what will be measured' line."""
        if self.slice_index is not None:
            return "slice %d" % self.slice_index
        parts = ["c%d" % self.channel]
        if self.z and self.z > 1:
            parts.append("z%d" % self.z)
        if self.t and self.t > 1:
            parts.append("t%d" % self.t)
        return " ".join(parts)

    def __eq__(self, other):
        return isinstance(other, Plane) and self.as_dict() == other.as_dict()

    def __ne__(self, other):
        # Python 2 does not derive __ne__ from __eq__.
        return not self.__eq__(other)

    def __repr__(self):
        return "Plane(%s, %s)" % (self.image_key, self.describe())


class Resolution(object):
    """The answer to "where is fish N's GFP?", plus why."""

    def __init__(self, plane, origin=ORIGIN_MANIFEST, expected=None,
                 channel_name=None):
        self.plane = plane
        self.origin = origin
        self.expected = expected          # the manifest's own answer, if overridden
        self.channel_name = channel_name

    @property
    def overridden(self):
        return self.origin != ORIGIN_MANIFEST

    def as_row_fields(self):
        """The IMAGE_KEYS provenance an override must leave in the CSV."""
        return {
            "PlaneOverride": self.overridden,
            "PlaneRecorded": self.plane.describe() if self.plane else "",
            "PlaneExpected": self.expected.describe() if self.expected else "",
        }

    def __repr__(self):
        return "Resolution(%r, origin=%r)" % (self.plane, self.origin)


# ---------------------------------------------------------------------------
#  Per-position plane math
# ---------------------------------------------------------------------------
#
# Pure functions computing the planes for ONE position (one hyperstack T/Z
# index, or one flat-stack slice-block), independent of any fixed fish count.
# The interactive review flow calls these directly, per position, as the
# operator confirms each one -- unlike Manifest.for_hyperstack/for_flat_stack
# above (which still exist for callers happy to assume exactly one fish per
# position, and are now thin loops over these same functions), review can
# offer more than one fish at a position without either constructor needing
# to know about that: it just calls set_plane() once per co-located fish
# with the identical Plane dict this function returns.

def hyperstack_position_planes(image_key, channel_indices, position,
                               fish_dimension="t", image_title=None):
    """{channel_name: Plane} for ONE T (or Z) position of a hyperstack.

    `channel_indices` maps channel_name -> 1-based channel (c) index.
    `position` is the 1-based T (or Z) index, matching ImageJ's convention.
    `fish_dimension` selects which axis `position` addresses ("t" or "z");
    the other axis is fixed at 1.
    """
    planes = {}
    for name, index in channel_indices.items():
        if fish_dimension == "z":
            planes[name] = Plane(image_key, channel=index, z=position, t=1,
                                 image_title=image_title)
        else:
            planes[name] = Plane(image_key, channel=index, z=1, t=position,
                                 image_title=image_title)
    return planes


def flat_stack_position_planes(image_key, slice_order, first_slice, position,
                               image_title=None):
    """{channel_name: Plane} for ONE slice-block of a flat stack.

    `slice_order` is the channel order within a block (stride = its length).
    A slot may be None -- a slice that was physically imaged but isn't any
    real channel (e.g. an unused detector). It still occupies a slot and
    counts toward the stride, so every later fish stays aligned; it just
    produces no dict entry.
    `first_slice` is the 1-based slice index of position 1's first slice.
    `position` is the 1-based block index.

    Purely arithmetic: does not check the result against a real stack's
    actual slice count. The caller (the interactive review flow) is
    responsible for not offering a position past the end of the real stack.
    """
    stride = len(slice_order)
    base = first_slice + (position - 1) * stride
    return dict((name, Plane(image_key, slice_index=base + offset,
                             image_title=image_title))
               for offset, name in enumerate(slice_order) if name is not None)


class Manifest(object):
    """fish -> channel -> Plane, with recorded per-fish corrections."""

    def __init__(self, layout, channel_names, bf_name):
        if layout not in LAYOUTS:
            raise ValueError("unknown layout %r" % (layout,))
        self.layout = layout
        self.channel_names = list(channel_names)
        self.bf_name = bf_name
        # {fish_ordinal: {channel_name: Plane}}
        self._planned = {}
        # {fish_ordinal: {channel_name: (Plane, origin)}}
        self._overrides = {}

    # -- construction -----------------------------------------------------

    def set_plane(self, fish, channel_name, plane):
        self._planned.setdefault(fish, {})[channel_name] = plane
        return plane

    @classmethod
    def for_hyperstack(cls, image_key, channel_names, bf_name, channel_indices,
                       fish_count, fish_dimension="t", image_title=None):
        """One fish per frame (or per Z), channels along the channel axis.

        This is the layout where automatic navigation is safe, because the
        channel axis genuinely means channel. A thin loop over
        hyperstack_position_planes(), kept only for callers happy to assume
        exactly one fish per position -- the interactive review flow instead
        calls hyperstack_position_planes() directly, per position, so it can
        offer more than one fish at a position without this constructor
        needing to know about that.
        """
        manifest = cls(LAYOUT_HYPERSTACK, channel_names, bf_name)
        for fish in range(1, fish_count + 1):
            planes = hyperstack_position_planes(
                image_key, channel_indices, fish, fish_dimension=fish_dimension,
                image_title=image_title)
            for name, plane in planes.items():
                manifest.set_plane(fish, name, plane)
        return manifest

    @classmethod
    def for_flat_stack(cls, image_key, channel_names, bf_name, fish_count,
                       slice_order=None, first_slice=1, image_title=None):
        """One plain stack, fish laid out consecutively.

        `slice_order` gives the channel order within each fish's block; it
        defaults to `channel_names`. Fish N's block therefore starts at
        ``first_slice + (N-1) * len(slice_order)``. A thin loop over
        flat_stack_position_planes(), for the same reason as for_hyperstack
        above.

        This is the layout the legacy tool could not navigate safely. Making the
        stride explicit here is what makes it safe: the assumption is stated
        once, written to disk, shown in the panel before each measurement, and
        correctable per fish when a block is out of order.
        """
        order = list(slice_order or channel_names)
        manifest = cls(LAYOUT_FLAT_STACK, channel_names, bf_name)
        for fish in range(1, fish_count + 1):
            planes = flat_stack_position_planes(
                image_key, order, first_slice, fish, image_title=image_title)
            for name, plane in planes.items():
                manifest.set_plane(fish, name, plane)
        return manifest

    @classmethod
    def for_per_window(cls, channel_images, channel_names, bf_name, fish_count,
                       image_titles=None):
        """One single-plane window per channel, shared by every fish.

        `channel_images` maps channel name -> image key. Every fish resolves to
        the same planes; which fish is on screen is the operator's business, so
        this layout leans hardest on the override path.
        """
        titles = image_titles or {}
        manifest = cls(LAYOUT_PER_WINDOW, channel_names, bf_name)
        for fish in range(1, fish_count + 1):
            for name in channel_names:
                if name not in channel_images:
                    continue
                manifest.set_plane(fish, name,
                                   Plane(channel_images[name],
                                         image_title=titles.get(name)))
        return manifest

    # -- resolution -------------------------------------------------------

    def resolve(self, fish, channel_name):
        """Where to measure, and whether that differs from the plan."""
        planned = self._planned.get(fish, {}).get(channel_name)
        override = self._overrides.get(fish, {}).get(channel_name)
        if override is not None:
            plane, origin = override
            return Resolution(plane, origin=origin, expected=planned,
                              channel_name=channel_name)
        return Resolution(planned, origin=ORIGIN_MANIFEST,
                          channel_name=channel_name)

    def describe(self, fish, channel_name):
        """The line the panel shows before measuring: 'Fish 12 - GFP -> slice 47'."""
        resolution = self.resolve(fish, channel_name)
        if resolution.plane is None:
            return "Fish %d - %s -> (not mapped)" % (fish, channel_name)
        text = "Fish %d - %s -> %s" % (fish, channel_name,
                                       resolution.plane.describe())
        if resolution.overridden:
            text += " [overridden]"
        return text

    # -- corrections ------------------------------------------------------

    def override_plane(self, fish, channel_name, plane,
                       origin=ORIGIN_OVERRIDE):
        """Point one fish's channel at a different plane.

        Used by "measure the plane I am actually looking at" when the mapping is
        wrong for this fish. The planned plane is kept so the row can report
        both what was expected and what was measured.
        """
        self._overrides.setdefault(fish, {})[channel_name] = (plane, origin)
        return self.resolve(fish, channel_name)

    def relabel(self, fish, from_channel, to_channel):
        """Swap two channels for one fish, for the plainly mislabeled case.

        Both directions are rewritten, so a mislabeled pair does not leave one
        channel pointing at the other's plane.
        """
        first = self.resolve(fish, from_channel).plane
        second = self.resolve(fish, to_channel).plane
        if first is None or second is None:
            raise ValueError("both channels must be mapped before relabelling")
        self.override_plane(fish, from_channel, second, origin=ORIGIN_RELABEL)
        self.override_plane(fish, to_channel, first, origin=ORIGIN_RELABEL)
        return self.resolve(fish, from_channel), self.resolve(fish, to_channel)

    def promote_override(self, from_fish, channel_name, through_fish):
        """Apply one fish's correction to every later fish, up to `through_fish`.

        For when a mislabeling is systematic from some point onward -- a
        miscounted block, an acquisition restarted mid-plate -- rather than a
        one-off. Shifts each subsequent fish by the same slice delta instead of
        pointing them all at one plane, which would be nonsense.
        """
        resolution = self.resolve(from_fish, channel_name)
        if not resolution.overridden or resolution.expected is None:
            raise ValueError("fish %r has no override on %r to promote"
                             % (from_fish, channel_name))

        actual = resolution.plane
        expected = resolution.expected
        if actual.slice_index is None or expected.slice_index is None:
            # Non-flat layouts: carry the substituted plane across verbatim.
            delta = None
        else:
            delta = actual.slice_index - expected.slice_index

        changed = []
        for fish in range(from_fish + 1, through_fish + 1):
            planned = self._planned.get(fish, {}).get(channel_name)
            if planned is None:
                continue
            if delta is None:
                shifted = Plane(actual.image_key, actual.channel, actual.z,
                                actual.t, actual.slice_index,
                                actual.image_title)
            else:
                shifted = Plane(planned.image_key, planned.channel, planned.z,
                                planned.t, planned.slice_index + delta,
                                planned.image_title)
            self.override_plane(fish, channel_name, shifted)
            changed.append(fish)
        return changed

    def overrides_for(self, fish):
        return dict(self._overrides.get(fish, {}))

    def override_count(self):
        return sum(len(v) for v in self._overrides.values())

    def fish_count(self):
        """How many distinct fish have at least one planned channel --
        used to set the session's fish target from the finished review (or a
        reloaded manifest on resume) instead of a number typed blind."""
        return len(self._planned)

    def has_fish(self, fish):
        """True if this fish ordinal is part of the plan at all (regardless
        of which channels it has). Used during measurement to tell "this
        channel was deliberately omitted for a planned fish" apart from "this
        fish was never planned at all" -- the two need different handling
        when a channel resolves to nothing."""
        return fish in self._planned

    # -- persistence ------------------------------------------------------

    def to_dict(self):
        return {
            "layout": self.layout,
            "channel_names": list(self.channel_names),
            "brightfield": self.bf_name,
            "planned": dict(
                (str(fish), dict((name, plane.as_dict())
                                 for name, plane in channels.items()))
                for fish, channels in self._planned.items()),
            "overrides": dict(
                (str(fish), dict((name, {"plane": plane.as_dict(),
                                         "origin": origin})
                                 for name, (plane, origin) in channels.items()))
                for fish, channels in self._overrides.items()),
        }

    @classmethod
    def from_dict(cls, data):
        manifest = cls(data["layout"], data["channel_names"],
                       data.get("brightfield"))
        for fish_text, channels in (data.get("planned") or {}).items():
            fish = int(fish_text)
            for name, plane in channels.items():
                manifest.set_plane(fish, name, Plane.from_dict(plane))
        for fish_text, channels in (data.get("overrides") or {}).items():
            fish = int(fish_text)
            for name, entry in channels.items():
                manifest.override_plane(fish, name,
                                        Plane.from_dict(entry["plane"]),
                                        origin=entry.get("origin",
                                                         ORIGIN_OVERRIDE))
        return manifest

    def save(self, path):
        """Persisted as readable JSON so a systematically wrong layout can be
        fixed once in a text editor between sessions, instead of overridden
        fish by fish."""
        temp_path = path + ".tmp"
        handle = open(temp_path, "w")
        try:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        os.rename(temp_path, path)
        return path

    @classmethod
    def load(cls, path):
        handle = open(path, "r")
        try:
            return cls.from_dict(json.load(handle))
        finally:
            handle.close()


# ---------------------------------------------------------------------------
#  Plausibility check
# ---------------------------------------------------------------------------

# A brightfield plane is broadly, evenly lit; a fluorescence plane is mostly
# dark with a bright minority. The gap is large enough to catch the common
# mislabeling without needing to be clever.
BF_MIN_MEAN_FRACTION = 0.15


def plane_looks_like_brightfield(mean, value_max):
    """Rough class of a plane from its mean intensity alone."""
    if not value_max:
        return None
    return (mean / value_max) >= BF_MIN_MEAN_FRACTION


def check_plane_plausible(channel_name, bf_name, mean, value_max):
    """Warn when a plane looks unlike the channel it is labelled as.

    Deliberately a warning and never a block: this heuristic is wrong often
    enough that refusing to measure on it would be worse than the mislabeling it
    catches. It exists to make the common case visible before the data is
    written, not to be authoritative.
    """
    looks_bf = plane_looks_like_brightfield(mean, value_max)
    if looks_bf is None:
        return None
    should_be_bf = (channel_name == bf_name)
    if looks_bf == should_be_bf:
        return None
    if should_be_bf:
        return ("%s is set as brightfield but this plane looks like "
                "fluorescence (mean is only %.1f%% of full scale). Check the "
                "channel order." % (channel_name, 100.0 * mean / value_max))
    return ("%s is a fluorescence channel but this plane looks like "
            "brightfield (mean is %.1f%% of full scale). Check the channel "
            "order." % (channel_name, 100.0 * mean / value_max))
