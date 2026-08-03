"""The setup-time interactive review: build the manifest by looking at the
real data, not by answering blind text fields.

Jython-only, mirrors workflow.py's Session/Controller split: ReviewSession
holds the state (the position walk, the in-progress fish list), ReviewPanel
is the Swing view over it. Entry point is run_review(), called from
Zebrafish_Quant.py's main() in place of the old blind build_manifest() call
(startup.py only collects the preliminary stride-determining questions now).

Three modes share one fish-list model (see design notes in the approved
plan), differing only in how positions are generated and what the per-step
action does:

  LAYOUT_HYPERSTACK / LAYOUT_FLAT_STACK ("Auto"): positions are read from the
    REAL stack (imp.getNFrames()/getNSlices(), or slices-until-they-run-out),
    not a blindly typed count. Each position defaults to "1 fish, all
    configured channels" (manifest.hyperstack_position_planes /
    flat_stack_position_planes); the operator can Add another fish at a
    position (multi-fish-per-image) or move fish within a position with
    Move-Up/Move-Down. Reordering ACROSS positions is not offered: position
    order is the real stack's order, not something to override.

  LAYOUT_PER_WINDOW ("Manual"): positions are the operator's open windows.
    The per-step action is "assign this window's channel to a fish" (pick an
    existing fish or create a new one) rather than "how many fish here" --
    for_per_window's model (one window shared by many fish) already needs no
    fish-count. The running fish list can be freely reordered.

Fish numbering is provisional throughout review (display-only, computed live
from list position for the corner overlay) and only becomes real, sequential
ordinals once at build_manifest() -- see ReviewSession.build_manifest.
"""

from __future__ import division

import threading
import traceback

from java.awt import BorderLayout, Color, Dimension, Font, GridLayout
from java.awt.event import WindowAdapter
from javax.swing import (BorderFactory, BoxLayout, JButton, JFrame, JLabel,
                         JOptionPane, JPanel, SwingUtilities)
from javax.swing.border import EmptyBorder

from zfquant import fiji_io
from zfquant import manifest as manifest_mod
from zfquant.ui import _Click, apply_look_and_feel


PANEL_WIDTH = 380

NEW_FISH = "__new_fish__"

# A channel deliberately marked "this fish doesn't have it" -- distinct from
# None (undecided/not yet assigned). Brightfield is never offered this
# option: the eye ROI is load-bearing for the whole tool's math.
SKIPPED = "__skipped__"


def _channel_codes(channel_names):
    """One short, stable, deduplicated lowercase code per channel name, for
    the corner overlay -- 'BF' -> 'b', 'RFP' -> 'r', and so on. Falls back to
    more letters, then a positional 'c<N>', only if names collide."""
    codes = {}
    used = set()
    for index, name in enumerate(channel_names):
        length = 1
        candidate = (name[:length] or "?").lower()
        while candidate in used and length < len(name):
            length += 1
            candidate = name[:length].lower()
        if candidate in used:
            candidate = "c%d" % (index + 1)
        used.add(candidate)
        codes[name] = candidate
    return codes


class ReviewFishEntry(object):
    """One in-progress fish row.

    `uid` is a stable identity used for Remove/Move/re-lookup -- NOT a
    display number. Display numbers (and, eventually, real Manifest
    ordinals) are always derived live from list position, so add/remove/move
    never has to renumber anything.

    `position` is the AUTO-mode position (1-based int) this entry was
    created at, or None for a Manual-mode entry (which isn't tied to any one
    stack position).
    """

    def __init__(self, uid, position=None):
        self.uid = uid
        self.position = position
        self.channels = {}   # channel_name -> Plane or None

    def set_channel(self, name, plane):
        self.channels[name] = plane

    def is_complete(self, channel_names):
        return all(self.channels.get(name) is not None
                   for name in channel_names)

    def summary(self, channel_names):
        parts = []
        for name in channel_names:
            state = self.channels.get(name)
            if state is None:
                text = "(not yet)"
            elif state == SKIPPED:
                text = "(omitted)"
            else:
                text = state.describe()
            parts.append("%s=%s" % (name, text))
        return ", ".join(parts)


class ReviewSession(object):
    """Owns the position walk and the in-progress/committed fish list."""

    def __init__(self, mode, config, images):
        self.mode = mode
        self.config = config
        self.channel_names = list(config.channel_names)
        self.bf_name = config.bf_name
        self._codes = _channel_codes(self.channel_names)

        self._fish_entries = []
        self._uid_counter = 0
        self._labeled_image_ids = set()
        self.cancelled = False

        self._target_imp = None
        self._target_key = None
        self._target_title = ""
        self.total_positions = 0
        self._images = []

        # Flat stack needs the channel order and first slice before it can
        # compute a position sequence at all -- both used to be typed blind
        # in a startup.py dialog; now they're set by walking the operator
        # through an interactive setup screen first (see stack_setup_done /
        # set_first_slice_to_current / append_slice_order_channel /
        # confirm_stack_setup below), with the real stack visible the whole
        # time. Position-walk setup is deferred until that's confirmed.
        self.stack_setup_done = (mode != manifest_mod.LAYOUT_FLAT_STACK)
        self.first_slice_confirmed = False
        self.slice_order = []

        # Positions explicitly marked "no fish here" -- tracked separately
        # from "just has zero entries right now" so a later Back + "Accept &
        # Apply to Rest" sweeping forward over an already-skipped position
        # can't silently resurrect a fish there; only add_fish_at_current()
        # (an explicit change of mind) clears an entry from this set.
        self._skipped_positions = set()

        if mode == manifest_mod.LAYOUT_PER_WINDOW:
            self._images = [imp for imp in images
                            if not fiji_io.is_working_image(imp)]
            self.current_position_index = 0 if self._images else -1
        else:
            target = fiji_io.current_image()
            if target is None or fiji_io.is_working_image(target):
                target = images[0] if images else None
            if target is None:
                self.current_position_index = 0
                return
            self._target_imp = target
            self._target_key = target.getID()
            self._target_title = target.getTitle()

            if mode == manifest_mod.LAYOUT_HYPERSTACK:
                if config.fish_dimension == "z":
                    self.total_positions = target.getNSlices()
                else:
                    self.total_positions = target.getNFrames()
                self.current_position_index = 1 if self.total_positions >= 1 else 0
                if self.current_position_index:
                    self._ensure_default_entries(self.current_position_index)
            else:
                # LAYOUT_FLAT_STACK: total_positions/current_position_index
                # are set later, by confirm_stack_setup(), once the operator
                # has defined first_slice and slice_order interactively.
                self.current_position_index = 0

        if self.stack_setup_done:
            self._refresh_label()

    # -- position math (Auto modes only) -----------------------------------

    def _planes_for_position(self, position):
        if self.mode == manifest_mod.LAYOUT_HYPERSTACK:
            return manifest_mod.hyperstack_position_planes(
                self._target_key, self.config.channel_indices, position,
                fish_dimension=self.config.fish_dimension,
                image_title=self._target_title)
        if self.mode == manifest_mod.LAYOUT_FLAT_STACK:
            order = self.config.slice_order or self.channel_names
            return manifest_mod.flat_stack_position_planes(
                self._target_key, order, self.config.first_slice, position,
                image_title=self._target_title)
        return {}

    def preview_plane_for(self, position):
        """Which plane to actually navigate to and show for `position` --
        the brightfield channel if configured, else the first channel."""
        planes = self._planes_for_position(position)
        if not planes:
            return None
        if self.bf_name in planes:
            return planes[self.bf_name]
        return planes[self.channel_names[0]]

    # -- interactive stack setup (LAYOUT_FLAT_STACK only) --------------------
    #
    # Replaces the old blind "type the channel order, type the first slice"
    # dialog: the operator navigates the REAL, visible stack (this panel is
    # non-modal, so nothing stops them scrolling it) to confirm both instead
    # of typing either one.

    def set_first_slice_to_current(self):
        """Whatever slice the operator currently has the real stack scrolled
        to becomes fish 1's first slice."""
        if self._target_imp is None:
            return None
        slice_index = self._target_imp.getCurrentSlice()
        self.config.first_slice = slice_index
        self.first_slice_confirmed = True
        return slice_index

    def append_slice_order_channel(self, name):
        """Click a channel button to add it as the next slice in the order,
        one click per channel instead of typing a comma-separated list.

        `name` may be None -- a slice that really was imaged but isn't any
        of the configured channels (a dead/unused detector slot, say).
        Unlike a real channel it can be clicked more than once, since a
        block can have more than one such slot.
        """
        if name is not None and name in self.slice_order:
            return
        self.slice_order.append(name)

    def undo_slice_order(self):
        if self.slice_order:
            self.slice_order.pop()

    def reset_slice_order(self):
        self.slice_order = []

    def slice_order_complete(self):
        used = [n for n in self.slice_order if n is not None]
        return sorted(used) == sorted(self.channel_names)

    def confirm_stack_setup(self):
        """Finish the interactive stack-setup step and start the normal
        position walk. Returns False (no-op) if first_slice/slice_order
        aren't both set yet."""
        if not self.first_slice_confirmed or not self.slice_order_complete():
            return False
        self.config.slice_order = list(self.slice_order)
        stride = len(self.slice_order)
        total_slices = self._target_imp.getStackSize()
        self.total_positions = max(
            0, (total_slices - self.config.first_slice + 1) // stride)
        self.stack_setup_done = True
        self.current_position_index = 1 if self.total_positions >= 1 else 0
        if self.current_position_index:
            self._ensure_default_entries(self.current_position_index)
        self._refresh_label()
        return True

    # -- fish list -----------------------------------------------------------

    def _next_uid(self):
        self._uid_counter += 1
        return self._uid_counter

    def _entry_by_uid(self, uid):
        for entry in self._fish_entries:
            if entry.uid == uid:
                return entry
        return None

    def _insertion_index_for(self, position):
        """Keep the global list sorted by position (Auto modes), so final
        list order always matches the real stack order."""
        last = -1
        for index, entry in enumerate(self._fish_entries):
            if entry.position is not None and entry.position <= position:
                last = index
        return last + 1

    def _ensure_default_entries(self, position):
        if position in self._skipped_positions:
            return
        if any(e.position == position for e in self._fish_entries):
            return
        planes = self._planes_for_position(position)
        if not planes:
            return
        entry = ReviewFishEntry(self._next_uid(), position=position)
        for name, plane in planes.items():
            entry.set_channel(name, plane)
        self._fish_entries.insert(self._insertion_index_for(position), entry)

    def add_fish_at_current(self):
        """Auto modes: add another fish co-located at the current position
        (multi-fish-per-image). No-op for Manual -- use assign_channel with
        NEW_FISH instead. An explicit add is a change of mind about a
        position previously marked skipped, so it un-skips it."""
        if self.mode == manifest_mod.LAYOUT_PER_WINDOW:
            return None
        position = self.current_position_index
        self._skipped_positions.discard(position)
        planes = self._planes_for_position(position)
        entry = ReviewFishEntry(self._next_uid(), position=position)
        for name, plane in planes.items():
            entry.set_channel(name, plane)
        self._fish_entries.insert(self._insertion_index_for(position), entry)
        return entry

    def remove_fish(self, uid):
        self._fish_entries = [e for e in self._fish_entries if e.uid != uid]

    def skip_current_position(self):
        """Auto modes: this position has no fish at all -- a blank frame, a
        failed acquisition, a gap. Clears every entry at the current position
        in one click, rather than removing them one at a time, and remembers
        that this position was deliberately left empty so a later bulk
        "Accept & Apply to Rest" can't silently resurrect a fish here."""
        if self.mode == manifest_mod.LAYOUT_PER_WINDOW:
            return
        position = self.current_position_index
        self._fish_entries = [e for e in self._fish_entries
                              if e.position != position]
        self._skipped_positions.add(position)

    def move_fish(self, uid, delta):
        """delta=+1/-1. Auto modes only allow swapping within the SAME
        position -- physical stack order isn't something to override, only
        "which fish is 1 vs 2 here" is. Manual allows free reordering."""
        entries = self._fish_entries
        index = None
        for i, entry in enumerate(entries):
            if entry.uid == uid:
                index = i
                break
        if index is None:
            return
        target = index + delta
        if target < 0 or target >= len(entries):
            return
        if (self.mode != manifest_mod.LAYOUT_PER_WINDOW
                and entries[index].position != entries[target].position):
            return
        entries[index], entries[target] = entries[target], entries[index]

    def assign_channel(self, fish_selector, channel_name, plane):
        """Manual mode's step action. `fish_selector` is an existing entry's
        uid, or the NEW_FISH sentinel."""
        if fish_selector == NEW_FISH:
            entry = ReviewFishEntry(self._next_uid(), position=None)
            self._fish_entries.append(entry)
        else:
            entry = self._entry_by_uid(fish_selector)
            if entry is None:
                return None
        entry.set_channel(channel_name, plane)
        return entry

    def toggle_channel_skip(self, uid, channel_name):
        """A fish may genuinely not have every configured channel (e.g. no
        GFP for this one). Toggling omits/restores `channel_name` for one
        fish; brightfield is not offered this in the UI since the eye ROI is
        required everywhere.

        Restoring an Auto-mode fish's channel re-fetches its position's
        planned plane (the default it started with); a Manual-mode fish has
        no position to restore from, so it goes back to undecided (None) and
        needs assigning again from a window.
        """
        entry = self._entry_by_uid(uid)
        if entry is None:
            return
        if entry.channels.get(channel_name) == SKIPPED:
            if entry.position is not None:
                planes = self._planes_for_position(entry.position)
                entry.channels[channel_name] = planes.get(channel_name)
            else:
                entry.channels[channel_name] = None
        else:
            entry.channels[channel_name] = SKIPPED

    def entries_at_current_position(self):
        if self.mode == manifest_mod.LAYOUT_PER_WINDOW:
            return list(self._fish_entries)
        return [e for e in self._fish_entries
               if e.position == self.current_position_index]

    def entries_referencing_current_window(self):
        if not self._images or self.current_position_index < 0:
            return []
        window_key = self._images[self.current_position_index].getID()
        matches = []
        for entry in self._fish_entries:
            for plane in entry.channels.values():
                if plane is not None and plane.image_key == window_key:
                    matches.append(entry)
                    break
        return matches

    def display_number(self, entry):
        """1-based position in the CURRENT list order -- provisional, purely
        for the live overlay/panel; final ordinals are only assigned once,
        in build_manifest()."""
        try:
            return self._fish_entries.index(entry) + 1
        except ValueError:
            return 0

    def current_window(self):
        if self.mode != manifest_mod.LAYOUT_PER_WINDOW:
            return None
        if 0 <= self.current_position_index < len(self._images):
            return self._images[self.current_position_index]
        return None

    # -- navigation -----------------------------------------------------------

    def go_next(self):
        if self.mode == manifest_mod.LAYOUT_PER_WINDOW:
            if self.current_position_index >= len(self._images) - 1:
                return False
            self.current_position_index += 1
        else:
            if self.current_position_index >= self.total_positions:
                return False
            self.current_position_index += 1
            self._ensure_default_entries(self.current_position_index)
        self._refresh_label()
        return True

    def go_back(self):
        lower_bound = 0 if self.mode == manifest_mod.LAYOUT_PER_WINDOW else 1
        if self.current_position_index <= lower_bound:
            return False
        self.current_position_index -= 1
        self._refresh_label()
        return True

    def apply_template_range(self):
        """Auto modes' fast path: fill every remaining position with the
        default (1 fish, all channels) template and land on the last one,
        still visually confirmed -- calls the exact same
        _ensure_default_entries() single-step Next uses, just in a loop, so
        the fast path can never drift from single-step behaviour. Back
        always works afterward to single-step-correct any touched position."""
        if self.mode == manifest_mod.LAYOUT_PER_WINDOW:
            return
        start = self.current_position_index + 1
        for position in range(start, self.total_positions + 1):
            self._ensure_default_entries(position)
        if self.total_positions >= 1:
            self.current_position_index = self.total_positions
        self._refresh_label()

    # -- completion -----------------------------------------------------------

    def is_complete(self):
        if not self._fish_entries:
            return False
        return all(e.is_complete(self.channel_names) for e in self._fish_entries)

    def build_manifest(self):
        """Real, sequential ordinals assigned exactly once, here, by walking
        the finished list in its final order. Only real Planes are written --
        both None (shouldn't happen, is_complete() already gates this) and
        SKIPPED (a fish that genuinely doesn't have this channel) leave that
        (fish, channel) simply absent from the manifest, which is exactly
        what workflow.py's arm() checks for via Manifest.has_fish()."""
        manifest = manifest_mod.Manifest(self.mode, self.channel_names,
                                         self.bf_name)
        for ordinal, entry in enumerate(self._fish_entries, start=1):
            for name, plane in entry.channels.items():
                if isinstance(plane, manifest_mod.Plane):
                    manifest.set_plane(ordinal, name, plane)
        return manifest

    # -- overlay label ----------------------------------------------------

    def _label_text_for(self, entries):
        parts = []
        for entry in entries:
            number = self.display_number(entry)
            for name in self.channel_names:
                if entry.channels.get(name) is not None:
                    parts.append("%d%s" % (number, self._codes[name]))
        return " ".join(parts)

    def _refresh_label(self):
        try:
            if self.mode == manifest_mod.LAYOUT_PER_WINDOW:
                imp = self.current_window()
                if imp is None:
                    return
                fiji_io.focus(imp)
                text = self._label_text_for(
                    self.entries_referencing_current_window())
                fiji_io.set_position_label(imp, text or "(unassigned)")
                self._labeled_image_ids.add(imp.getID())
            else:
                plane = self.preview_plane_for(self.current_position_index)
                imp = fiji_io.seek_to(plane) if plane else self._target_imp
                if imp is None:
                    return
                text = self._label_text_for(self.entries_at_current_position())
                fiji_io.set_position_label(imp, text)
                self._labeled_image_ids.add(imp.getID())
        except Exception:
            fiji_io.log("Review label refresh failed: " + traceback.format_exc())

    def cleanup(self):
        """Clear every overlay label review ever drew, so nothing leaks into
        the measurement phase (or a later export_audit_image overlay on the
        same images)."""
        for image_id in self._labeled_image_ids:
            imp = fiji_io.image_by_key(image_id)
            if imp is not None:
                fiji_io.clear_position_label(imp)
        self._labeled_image_ids = set()


# ---------------------------------------------------------------------------
#  Swing panel
# ---------------------------------------------------------------------------

class ReviewPanel(object):

    def __init__(self, session, on_finish):
        self.session = session
        self.on_finish = on_finish
        self.frame = None
        self.position_label = None
        self.content_panel = None
        self.done_button = None
        self.apply_button = None
        self.back_button = None
        self.next_button = None
        self._pending_channel = None   # Manual mode: channel picked, waiting
                                       # for a fish click to complete the
                                       # assignment -- see _manual_channel_row
                                       # / _manual_fish_picker_row
        self._build()

    def _build(self):
        apply_look_and_feel()

        frame = JFrame("Zebrafish Quant - Review")
        frame.setDefaultCloseOperation(JFrame.DO_NOTHING_ON_CLOSE)
        frame.setAlwaysOnTop(True)

        root = JPanel()
        root.setLayout(BoxLayout(root, BoxLayout.Y_AXIS))
        root.setBorder(EmptyBorder(10, 10, 10, 10))

        self.position_label = self._label("", bold=True, size=13)
        root.add(self.position_label)
        root.add(self._spacer())

        self.content_panel = JPanel()
        self.content_panel.setLayout(BoxLayout(self.content_panel,
                                               BoxLayout.Y_AXIS))
        root.add(self.content_panel)
        root.add(self._spacer())

        hint = JLabel(
            "<html><body style='width:%dpx'>Back / Next move between "
            "positions in the stack (or open windows, in Manual). Move "
            "Up/Down only matter when more than one fish shares a "
            "position.</body></html>" % (PANEL_WIDTH - 20))
        hint.setFont(Font("SansSerif", Font.PLAIN, 10))
        hint.setForeground(Color(0x80, 0x80, 0x80))
        root.add(hint)
        root.add(self._spacer())

        nav = JPanel(GridLayout(0, 2, 4, 4))

        self.back_button = self._button("< Back", self._on_back)
        self.back_button.setToolTipText(
            "Go to the previous position (or window, in Manual mode) to "
            "review or change it. Nothing already reviewed is lost.")
        nav.add(self.back_button)

        self.next_button = self._button("Next >", self._on_next)
        self.next_button.setToolTipText(
            "Accept what's shown for this position as-is and move to the "
            "next one.")
        nav.add(self.next_button)

        self.apply_button = self._button("Accept & Apply to Rest",
                                         self._on_apply_range)
        self.apply_button.setToolTipText(
            "Fast-forward: apply the default (1 fish, every channel) to "
            "every remaining position without reviewing each one. Lands on "
            "the last position so you can spot-check it, and Back still "
            "works to fix anything individually.")
        nav.add(self.apply_button)

        cancel = self._button("Cancel", self._on_cancel)
        cancel.setToolTipText("Abandon setup. Nothing has been measured yet.")
        nav.add(cancel)
        root.add(nav)

        root.add(self._spacer())
        self.done_button = self._button("Done", self._on_done)
        self.done_button.setToolTipText(
            "Finish review and start measuring. Enabled once every fish has "
            "every channel either assigned or omitted.")
        root.add(self.done_button)

        frame.getContentPane().add(root, BorderLayout.CENTER)
        frame.addWindowListener(_ReviewCloseGuard(self))
        frame.pack()
        frame.setSize(PANEL_WIDTH + 40, max(360, frame.getHeight()))
        frame.setLocation(420, 80)
        frame.setVisible(True)
        self.frame = frame
        self.refresh()

    def _label(self, text, bold=False, size=12):
        label = JLabel(text)
        style = Font.BOLD if bold else Font.PLAIN
        label.setFont(Font("SansSerif", style, size))
        return label

    def _spacer(self):
        panel = JPanel()
        panel.setPreferredSize(Dimension(1, 8))
        return panel

    def _button(self, text, callback):
        button = JButton(text)
        button.setFocusable(False)
        button.addActionListener(_Click(callback))
        return button

    # -- navigation handlers ------------------------------------------------

    def _on_back(self):
        self._pending_channel = None   # doesn't carry over between windows
        self.session.go_back()
        self.refresh()

    def _on_next(self):
        self._pending_channel = None
        self.session.go_next()
        self.refresh()

    def _on_apply_range(self):
        self.session.apply_template_range()
        self.refresh()

    def _on_done(self):
        # _Click swallows and logs exceptions rather than re-raising, so
        # anything that throws below without reaching _finish() would leave
        # run_review()'s done_event unset forever -- the calling thread (all
        # of Fiji's script execution) would hang with no way to recover
        # short of force-quitting. Guarantee _finish() always runs.
        try:
            if not self.session.is_complete():
                JOptionPane.showMessageDialog(
                    self.frame,
                    "At least one fish needs every channel assigned before "
                    "finishing review.",
                    "Zebrafish Quant - Review", JOptionPane.WARNING_MESSAGE)
                return
            manifest = self.session.build_manifest()
        except Exception:
            fiji_io.log("Failed to build the manifest from review: "
                        + traceback.format_exc())
            JOptionPane.showMessageDialog(
                self.frame,
                "Could not build the plan from what was reviewed; see the "
                "Log. Setup was cancelled.",
                "Zebrafish Quant - Review", JOptionPane.ERROR_MESSAGE)
            self._finish(None)
            return
        self.session.cleanup()
        self._finish(manifest)

    def _on_cancel(self):
        try:
            choice = JOptionPane.showConfirmDialog(
                self.frame, "Cancel setup? Nothing has been measured yet.",
                "Zebrafish Quant - Review", JOptionPane.YES_NO_OPTION)
            if choice != JOptionPane.YES_OPTION:
                return
            self.session.cancelled = True
            self.session.cleanup()
        except Exception:
            fiji_io.log("Error while cancelling review: "
                        + traceback.format_exc())
        self._finish(None)

    def _finish(self, manifest):
        try:
            if self.frame is not None:
                self.frame.dispose()
        finally:
            self.on_finish(manifest)

    # -- rendering ------------------------------------------------------------

    def refresh(self):
        self._later(self._refresh_now)

    def _refresh_now(self):
        session = self.session
        if (session.mode == manifest_mod.LAYOUT_FLAT_STACK
                and not session.stack_setup_done):
            self._render_stack_setup()
            return
        self.back_button.setEnabled(True)
        self.next_button.setEnabled(True)
        if session.mode == manifest_mod.LAYOUT_PER_WINDOW:
            self._render_manual()
        else:
            self._render_auto()
        self.done_button.setEnabled(session.is_complete())

    def _render_stack_setup(self):
        """Auto Single Stack only: confirm the channel order and first slice
        by looking at the real stack and clicking, instead of typing either
        one blind. Back/Next/Apply/Done are all disabled here -- there is no
        position sequence to walk yet."""
        session = self.session
        self.position_label.setText("Auto Single Stack -- set up the stack")
        self.apply_button.setVisible(False)
        self.back_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.done_button.setEnabled(False)

        self.content_panel.removeAll()

        self.content_panel.add(self._heading(
            "1. Scroll the stack to fish 1's first slice, then click:"))
        first_text = ("Fish 1 starts at slice %d" % session.config.first_slice
                     if session.first_slice_confirmed
                     else "(not set yet -- scroll the stack, then click below)")
        self.content_panel.add(self._button(
            "Use current slice as fish 1's start", self._on_set_first_slice))
        self.content_panel.add(self._label(first_text, size=11))
        self.content_panel.add(self._spacer())

        self.content_panel.add(self._heading(
            "2. Click channels in the order they appear in the stack. If a "
            "slice was imaged but isn't one of your channels (e.g. it was "
            "captured but has no signal), click Skip for that slice so the "
            "positions after it still line up:"))
        order_row = JPanel(GridLayout(0, 1, 2, 2))
        for name in session.channel_names:
            used = name in session.slice_order
            button = self._button(name + (" (used)" if used else ""),
                                  self._make_slice_order_callback(name))
            button.setEnabled(not used)
            order_row.add(button)
        order_row.add(self._button(
            "Skip (imaged, not a channel)", self._make_slice_order_callback(None)))
        self.content_panel.add(order_row)

        order_text = "Order so far: %s" % (
            ", ".join(n if n is not None else "(skip)"
                     for n in session.slice_order) or "(none yet)")
        self.content_panel.add(self._label(order_text, size=11))

        controls = JPanel(GridLayout(1, 0, 4, 4))
        controls.add(self._button("Undo last", self._on_undo_slice_order))
        controls.add(self._button("Reset", self._on_reset_slice_order))
        self.content_panel.add(controls)

        self.content_panel.add(self._spacer())
        ready = session.first_slice_confirmed and session.slice_order_complete()
        confirm = self._button("Confirm setup, start reviewing",
                               self._on_confirm_stack_setup)
        confirm.setEnabled(ready)
        self.content_panel.add(confirm)

        self.content_panel.revalidate()
        self.content_panel.repaint()
        if self.frame is not None:
            self.frame.pack()

    def _render_auto(self):
        session = self.session
        mode_name = ("Auto Hyperstack" if session.mode
                    == manifest_mod.LAYOUT_HYPERSTACK else "Auto Single Stack")
        self.position_label.setText(
            "%s -- position %d of %d" % (mode_name,
                                         session.current_position_index,
                                         session.total_positions))
        self.apply_button.setVisible(True)
        self.apply_button.setEnabled(True)

        self.content_panel.removeAll()
        self.content_panel.add(self._heading("Fish at this position"))
        for entry in session.entries_at_current_position():
            self.content_panel.add(self._fish_row(entry))

        actions = JPanel(GridLayout(0, 1, 2, 2))
        actions.add(self._button("+ Add another fish at this position",
                                 self._make_add_callback()))
        skip = self._button("Skip this position (no fish here)",
                            self._on_skip_position)
        skip.setToolTipText("Blank frame, failed acquisition, a gap in the "
                            "stack -- clears every fish at this position in "
                            "one click.")
        actions.add(skip)
        self.content_panel.add(actions)

        self.content_panel.revalidate()
        self.content_panel.repaint()
        if self.frame is not None:
            self.frame.pack()

    def _render_manual(self):
        session = self.session
        imp = session.current_window()
        title = imp.getTitle() if imp is not None else "(no images open)"
        self.position_label.setText(
            "Manual -- window %d of %d: %s"
            % (session.current_position_index + 1, len(session._images),
              title))
        self.apply_button.setVisible(False)

        self.content_panel.removeAll()
        self.content_panel.add(self._heading(
            "1. Click which channel this window is"))
        self.content_panel.add(self._manual_channel_row())
        self.content_panel.add(self._spacer())

        if self._pending_channel is not None:
            self.content_panel.add(self._heading(
                "2. Click which fish gets %s" % self._pending_channel))
            self.content_panel.add(self._manual_fish_picker_row())
        else:
            self.content_panel.add(self._label(
                "Pick a channel above, then click the fish it belongs to.",
                size=11))

        self.content_panel.add(self._spacer())
        self.content_panel.add(self._heading("Fish so far"))
        for entry in session._fish_entries:
            self.content_panel.add(self._fish_row(entry))
        self.content_panel.revalidate()
        self.content_panel.repaint()
        if self.frame is not None:
            self.frame.pack()

    def _heading(self, text):
        label = self._label(text, bold=True, size=11)
        label.setForeground(Color(0x60, 0x60, 0x60))
        return label

    def _fish_row(self, entry):
        session = self.session
        outer = JPanel()
        outer.setLayout(BoxLayout(outer, BoxLayout.Y_AXIS))
        outer.setBorder(BorderFactory.createEmptyBorder(2, 0, 6, 0))

        top = JPanel(BorderLayout(6, 0))
        number = session.display_number(entry)
        text = "Fish %d: %s" % (number, entry.summary(session.channel_names))
        top.add(self._label(text, size=11), BorderLayout.CENTER)

        buttons = JPanel(GridLayout(1, 0, 2, 0))
        up = self._button("Move Up", self._make_move_callback(entry.uid, -1))
        up.setToolTipText("Move this fish earlier in the numbering -- only "
                          "matters when more than one fish shares a position.")
        down = self._button("Move Down", self._make_move_callback(entry.uid, 1))
        down.setToolTipText("Move this fish later in the numbering.")
        remove = self._button("Remove", self._make_remove_callback(entry.uid))
        remove.setToolTipText("Remove this fish from the plan entirely.")
        buttons.add(up)
        buttons.add(down)
        buttons.add(remove)
        top.add(buttons, BorderLayout.EAST)
        outer.add(top)

        # Per-channel omit/include toggles -- a fish genuinely may not have
        # every configured channel. Brightfield is never offered here: the
        # eye ROI is required for every fish.
        omittable = [n for n in session.channel_names if n != session.bf_name]
        if omittable:
            toggles = JPanel(GridLayout(1, 0, 2, 0))
            for name in omittable:
                skipped = entry.channels.get(name) == SKIPPED
                label = ("Include %s" % name) if skipped else ("Omit %s" % name)
                button = self._button(label, self._make_toggle_callback(
                    entry.uid, name))
                button.setToolTipText(
                    "This fish has no %s -- click to leave it out" % name
                    if not skipped else
                    "%s is currently omitted for this fish -- click to "
                    "include it again" % name)
                toggles.add(button)
            outer.add(toggles)
        return outer

    def _manual_channel_row(self):
        """Step 1: click which channel this window is. One click, no
        dropdown -- the selected channel is highlighted and step 2 appears
        below."""
        session = self.session
        row = JPanel(GridLayout(0, 1, 2, 2))
        for name in session.channel_names:
            selected = (self._pending_channel == name)
            label = ("> %s (selected)" % name) if selected else name
            button = self._button(label, self._make_pick_channel_callback(name))
            button.setToolTipText("This window shows the %s channel" % name)
            row.add(button)
        skip = self._button("This window isn't used",
                            self._make_pick_channel_callback(None))
        skip.setToolTipText("Clear the current selection; nothing is "
                            "assigned from this window.")
        row.add(skip)
        return row

    def _manual_fish_picker_row(self):
        """Step 2: click which fish gets the channel picked in step 1. The
        click itself completes the assignment -- no separate Assign button."""
        session = self.session
        row = JPanel(GridLayout(0, 1, 2, 2))
        for entry in session._fish_entries:
            label = "Fish %d" % session.display_number(entry)
            row.add(self._button(label, self._make_assign_callback(entry.uid)))
        row.add(self._button("+ New fish", self._make_assign_callback(NEW_FISH)))
        return row

    def _make_pick_channel_callback(self, name):
        def callback():
            self._pending_channel = name
            self.refresh()
        return callback

    def _make_assign_callback(self, fish_selector):
        def callback():
            if self._pending_channel is None:
                return
            session = self.session
            imp = session.current_window()
            if imp is None:
                return
            plane = fiji_io.plane_of(imp)   # already carries imp.getTitle()
            session.assign_channel(fish_selector, self._pending_channel, plane)
            self._pending_channel = None
            self.refresh()
        return callback

    def _make_add_callback(self):
        def callback():
            self.session.add_fish_at_current()
            self.refresh()
        return callback

    def _on_skip_position(self):
        self.session.skip_current_position()
        self.refresh()

    def _make_remove_callback(self, uid):
        def callback():
            self.session.remove_fish(uid)
            self.refresh()
        return callback

    def _make_move_callback(self, uid, delta):
        def callback():
            self.session.move_fish(uid, delta)
            self.refresh()
        return callback

    def _make_toggle_callback(self, uid, channel_name):
        def callback():
            self.session.toggle_channel_skip(uid, channel_name)
            self.refresh()
        return callback

    # -- interactive stack setup callbacks (LAYOUT_FLAT_STACK only) --------

    def _on_set_first_slice(self):
        self.session.set_first_slice_to_current()
        self.refresh()

    def _make_slice_order_callback(self, name):
        def callback():
            self.session.append_slice_order_channel(name)
            self.refresh()
        return callback

    def _on_undo_slice_order(self):
        self.session.undo_slice_order()
        self.refresh()

    def _on_reset_slice_order(self):
        self.session.reset_slice_order()
        self.refresh()

    def _on_confirm_stack_setup(self):
        if not self.session.confirm_stack_setup():
            JOptionPane.showMessageDialog(
                self.frame,
                "Set fish 1's first slice and click every channel once "
                "before continuing.",
                "Zebrafish Quant - Review", JOptionPane.WARNING_MESSAGE)
            return
        self.refresh()

    def _later(self, callback):
        def run():
            try:
                callback()
            except Exception:
                fiji_io.log(traceback.format_exc())
        SwingUtilities.invokeLater(run)


class _ReviewCloseGuard(WindowAdapter):

    def __init__(self, panel):
        self.panel = panel

    def windowClosing(self, event):
        self.panel._on_cancel()


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------

def run_review(mode, config, images):
    """Blocks the CALLING thread (the Fiji script-execution thread, not the
    EDT -- same assumption startup.py's existing GenericDialog.showDialog()
    calls already make) until the operator finishes or cancels review.
    Returns the built Manifest, or None if cancelled.
    """
    session = ReviewSession(mode, config, images)
    done_event = threading.Event()
    result = {"manifest": None}

    def on_finish(manifest):
        result["manifest"] = manifest
        done_event.set()

    ReviewPanel(session, on_finish)
    done_event.wait()
    return result["manifest"]
