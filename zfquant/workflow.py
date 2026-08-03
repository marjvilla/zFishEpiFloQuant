"""The per-fish state machine.

Imports fiji_io, so this is Jython-only. The rules it enforces are the ones the
audit found broken in the legacy tool:

  * arming a channel duplicates that one plane into a small standalone working
    image, and every draw/accept happens on that duplicate -- never on the
    live, scrollable stack. A single-plane image has nothing to scroll away
    from, so "the stack moved out from under me mid-draw" is not detected
    here, it is structurally impossible.
  * "skipped" is a recorded state, not the absence of a state
  * one commit path, not two copy-pasted ones
  * every commit goes through the journal first, so a crash cannot lose it
"""

from __future__ import division

import os
import threading
import traceback

from zfquant import core
from zfquant import fiji_io
from zfquant import journal as journal_mod
from zfquant import manifest as manifest_mod


# Per-channel phases. Background comes first so the threshold can be referenced
# to it -- that ordering is what makes signal areas comparable between fish.
PHASE_IDLE = "idle"
PHASE_EYE = "eye"
PHASE_BACKGROUND = "background"
PHASE_SIGNAL = "signal"

STATE_PENDING = "pending"
STATE_BG_SET = "BG set"
STATE_DONE = "done"
STATE_SKIPPED = "skipped"


class Capture(object):
    """One accepted ROI plus where and how it was measured."""

    def __init__(self, roi, plane, stats, resolution=None, threshold=None,
                 provenance=None, box=None):
        self.roi = roi
        self.plane = plane
        self.stats = stats
        self.resolution = resolution
        self.threshold = threshold
        self.provenance = provenance or {}
        self.box = box


class Session(object):
    """Everything about the run in progress."""

    def __init__(self, config, paths, manifest, journal):
        self.config = config
        self.paths = paths
        self.manifest = manifest
        self.journal = journal

        self.channel_names = list(config.channel_names)
        self.bf_name = config.bf_name
        self.fl_names = [n for n in self.channel_names if n != self.bf_name]
        self.operator = config.operator
        self.min_area = config.min_area
        self.k = config.k

        # Set from the finished interactive review's actual fish count, not a
        # blind guess -- see review.py. "Add another fish" beyond this still
        # works via the existing use_current_plane() override path.
        self.fish_target = config.fish_total
        self.fish_ordinal = journal.next_sequence()

        # Per-fish state, all keyed by channel name.
        self.captures = {}          # name -> Capture   (signal, or "Eye")
        self.backgrounds = {}       # name -> Capture
        self.skipped = set()
        self.finished = False

    # -- per-fish bookkeeping --------------------------------------------

    def reset_fish(self):
        self.captures = {}
        self.backgrounds = {}
        self.skipped = set()

    def state_of(self, name):
        if name in self.skipped:
            return STATE_SKIPPED
        # The brightfield/eye capture is always stored under the fixed key
        # "Eye" (see Controller._accept_eye), never under its own channel
        # name, so that key has to be translated here or this channel would
        # never show as done and Space would never auto-commit a fish.
        key = "Eye" if name == self.bf_name else name
        if key in self.captures:
            return STATE_DONE
        if name in self.backgrounds:
            return STATE_BG_SET
        return STATE_PENDING

    def progress(self):
        """(resolved, total) where a skip counts as resolved.

        The legacy counter only counted captures, so deliberately skipping the
        eye left the fish permanently incomplete and the commit-on-space
        affordance permanently unreachable.
        """
        total = len(self.channel_names)
        resolved = 0
        for name in self.channel_names:
            if self.state_of(name) in (STATE_DONE, STATE_SKIPPED):
                resolved += 1
        return resolved, total

    def all_resolved(self):
        resolved, total = self.progress()
        return resolved >= total

    def fish_label(self):
        return "Fish %d" % self.fish_ordinal

    def committed_count(self):
        return self.journal.live_fish_count()


class Controller(object):
    """Arming, accepting and committing. The panel calls into this."""

    def __init__(self, session):
        self.s = session
        self.panel = None
        self.archive = fiji_io.RoiArchive(session.paths.roi_zip)

        self._phase = PHASE_IDLE
        self._channel = None
        # The single-plane duplicate the operator is currently drawing on.
        # See open_channel()/fiji_io.open_working_copy for why this replaces
        # the old "did the live stack move" drift check outright.
        self._working_imp = None
        self._armed_plane = None       # the REAL source plane, for provenance
        self._resolution = None
        self._threshold = None
        self._committing = False
        # Stack of (label, undo_callable) for the CURRENT fish only: accepted
        # eye/background/signal captures and skips, most recent last. Cleared
        # whenever a fish is committed or rolled back. "Undo" pops this first
        # and only falls through to undoing an entire saved fish once it is
        # empty -- see undo().
        self._fish_undo = []

    # -- status -----------------------------------------------------------

    def status(self, text):
        if self.panel is not None:
            self.panel.set_status(text)
        fiji_io.log(text)

    def warn(self, text):
        if self.panel is not None:
            self.panel.set_warning(text)
        fiji_io.log("WARNING: " + text)

    def refresh(self):
        if self.panel is not None:
            self.panel.refresh()

    def describe_armed(self):
        """What's actually armed right now, for the panel's plane label.

        Reflects the live Controller state directly rather than re-resolving
        from the manifest: in freeform mode the "use whatever's current"
        fallback in arm() is never written back into the manifest (there is
        nothing to write -- using the current plane IS freeform's plan), so a
        fresh manifest lookup would print "(not mapped)" even while a real
        plane is armed and being drawn on.
        """
        if self._channel is None or self._armed_plane is None:
            return None
        return "%s -> %s" % (self._channel, self._armed_plane.describe())

    # -- arming -----------------------------------------------------------

    def arm(self, name):
        """Arm a channel: duplicate its plane into a working image and draw
        there, never on the live, scrollable stack."""
        session = self.s
        if session.finished:
            self.status("Session finished. Start a new one to keep measuring.")
            return

        self._close_working_image()   # abandon whatever the last arm left open

        resolution = session.manifest.resolve(session.fish_ordinal, name)

        if resolution.plane is None and session.manifest.has_fish(
                session.fish_ordinal):
            # This fish IS in the reviewed plan; it just doesn't have this
            # channel -- deliberately omitted during review (see review.py's
            # per-fish channel-omit toggle), not merely unmapped by accident.
            # Auto-mark it skipped rather than treating it as a drift needing
            # an override warning.
            session.skipped.add(name)
            self.status("%s was omitted for this fish during review. Marked "
                       "skipped -- pick another channel, or Space to "
                       "continue." % name)
            self.refresh()
            return

        source = fiji_io.seek_to(resolution.plane)
        if source is None:
            # No mapping for a fish that isn't even in the plan (e.g. an
            # "Add another fish" beyond what review produced), or the mapped
            # window was closed. Fall back to the front window rather than
            # refusing outright, but always record it as an override -- it
            # IS a deviation from the reviewed plan.
            source = fiji_io.current_image()
            if source is None or fiji_io.is_working_image(source):
                self.status("Open an image in Fiji first.")
                return
            resolution = manifest_mod.Resolution(
                fiji_io.plane_of(source), origin=manifest_mod.ORIGIN_OVERRIDE,
                expected=resolution.plane, channel_name=name)
            self.warn("%s is not mapped to an open image; using the front "
                      "window. This will be recorded as an override." % name)

        self._check_plausible(source, name)
        self._open_channel(name, source, resolution)
        self.refresh()

    def _open_channel(self, name, source, resolution):
        """Duplicate `resolution.plane` off `source` into a fresh working
        image and arm the first drawing step for `name` on it."""
        session = self.s
        stub = "%s_%s" % (session.fish_label().replace(" ", ""), name)
        working = fiji_io.open_working_copy(source, resolution.plane, stub)

        self._resolution = resolution
        self._channel = name
        self._armed_plane = resolution.plane
        self._working_imp = working

        if name == session.bf_name:
            self._arm_eye(working)
        else:
            self._arm_background(working, name)

    def _arm_eye(self, working):
        fiji_io.clear_threshold(working)  # never a red overlay on brightfield
        fiji_io.arm_tool(fiji_io.TOOL_OVAL)
        self._phase = PHASE_EYE
        self._restore_existing(working, "Eye")
        self.status("%s -- draw the EYE ellipse, then Space."
                    % self.s.manifest.describe(self.s.fish_ordinal,
                                               self.s.bf_name))

    def _arm_background(self, working, name):
        fiji_io.clear_threshold(working)
        fiji_io.arm_tool(fiji_io.TOOL_RECTANGLE)
        self._phase = PHASE_BACKGROUND
        self._restore_existing(working, "BG_" + name)
        self.status("%s -- drag a BACKGROUND box in a fish-free region, then "
                    "Space. The threshold is measured from it."
                    % self.s.manifest.describe(self.s.fish_ordinal, name))

    def _arm_signal(self, working, name):
        fiji_io.arm_tool(fiji_io.TOOL_RECTANGLE)
        self._phase = PHASE_SIGNAL
        working.deleteRoi()
        self.status("Drag a box around the %s signal, then Space. Everything "
                    "above threshold inside the box is selected. Adjusted the "
                    "threshold sliders? Use Threshold (T) first to lock that "
                    "value in." % name)

    def _close_working_image(self):
        if self._working_imp is not None:
            fiji_io.close_working_copy(self._working_imp)
            self._working_imp = None

    def _restore_existing(self, imp, key):
        """Show an already-captured ROI when revisiting a channel, so it can be
        adjusted rather than redrawn from scratch."""
        existing = self.s.captures.get(key) or self.s.backgrounds.get(
            key.replace("BG_", ""))
        if existing is not None and existing.roi is not None:
            imp.setRoi(existing.roi)
        else:
            imp.deleteRoi()

    def _check_plausible(self, imp, name):
        try:
            stats = fiji_io.measure(imp, _whole_image_roi(imp))
            warning = manifest_mod.check_plane_plausible(
                name, self.s.bf_name, stats["Mean"], fiji_io.value_max_for(imp))
            if warning:
                self.warn(warning)
            elif self.panel is not None:
                self.panel.set_warning("")
        except Exception:
            fiji_io.log("Plausibility check failed: " + traceback.format_exc())

    # -- accepting --------------------------------------------------------

    def accept(self):
        """Space. Accept what is drawn and move to the next phase."""
        session = self.s
        if self._phase == PHASE_IDLE:
            if session.all_resolved():
                self.commit()
            else:
                resolved, total = session.progress()
                self.status("Pick a channel first. (%d/%d resolved)"
                            % (resolved, total))
            return

        working = self._working_imp
        if working is None or not fiji_io.is_open(working):
            # The only way this can happen now: the operator closed the
            # working window by hand. There is no live-stack drift to check
            # for any more -- a single-plane duplicate has nothing to scroll.
            self.status("The working image was closed. Press the channel key "
                        "to re-arm.")
            self._phase = PHASE_IDLE
            self._channel = None
            return

        roi = working.getRoi()
        if roi is None:
            self.status("Nothing drawn yet.")
            return

        try:
            if self._phase == PHASE_EYE:
                self._accept_eye(working, roi)
            elif self._phase == PHASE_BACKGROUND:
                self._accept_background(working, roi)
            elif self._phase == PHASE_SIGNAL:
                self._accept_signal(working, roi)
        except Exception:
            self.status("Could not accept that selection; see the Log.")
            fiji_io.log(traceback.format_exc())
        self.refresh()

    def _accept_eye(self, working, roi):
        stats = fiji_io.measure(working, roi)
        capture = Capture(roi.clone(), self._armed_plane, stats,
                          resolution=self._resolution)
        capture.provenance["FocusScore"] = fiji_io.focus_score(working, roi)
        self.s.captures["Eye"] = capture
        self.s.skipped.discard(self.s.bf_name)
        self._close_working_image()
        self._phase = PHASE_IDLE

        roi_names, image_path = self._export_eye()
        self._push_undo("the eye", lambda: self._undo_capture(
            "Eye", self.s.bf_name, roi_names, image_path))
        self._after_channel()

    def _accept_background(self, working, roi):
        stats = fiji_io.measure(working, roi)
        self.s.backgrounds[self._channel] = Capture(
            roi.clone(), self._armed_plane, stats,
            resolution=self._resolution)

        threshold = core.resolve_threshold(
            bg_stats=stats,
            hist=fiji_io.histogram_of(working, roi),
            k=self.s.k,
            value_max=fiji_io.value_max_for(working))

        if threshold is None:
            self.warn("Could not derive a threshold from that background box. "
                      "Try a larger or less uniform region.")
            self.s.backgrounds.pop(self._channel, None)
            return

        self._threshold = threshold
        name = self._channel

        roi_names, _ = self._export_background_only(name)
        self._push_undo("background for %s" % name,
                        lambda: self._undo_background(name, roi_names))

        # Opens the real, draggable Threshold window as a VISUAL aid -- the
        # operator can see the red overlay and drag the sliders. But its
        # displayed value is not trusted passively: ImageJ's Threshold window
        # can silently recompute its own default on any later image-update
        # event (drawing the signal box counts), not just on opening, so
        # reading it back lazily at accept time is not reliable -- it was
        # observed reverting to (0, ~box max) between arming and accepting,
        # which is not a threshold at all and slipped an unthresholded box
        # into real data. self._threshold (set above, used as-is by
        # _accept_signal) is the value actually applied. An operator who
        # wants to use their own adjusted sliders instead has to explicitly
        # lock it in via capture_threshold() -- see its docstring.
        fiji_io.open_threshold_window(working, threshold)
        self._arm_signal(working, name)

    def capture_threshold(self):
        """Explicit "use what I see" action for the signal step: locks in
        whatever the Threshold window's sliders show RIGHT NOW as the
        threshold for the box about to be accepted.

        This exists instead of _accept_signal silently reading back the
        live processor threshold, because that passive read is exactly what
        was unreliable -- ImageJ's Threshold window can recompute its own
        default on any image-update event that happens between arming and
        accepting, not only on opening, so "whatever is on the processor by
        the time Space is pressed" is not trustworthy. Capturing it here, at
        the exact moment the operator asks for it, has no such gap.
        """
        if self._phase != PHASE_SIGNAL:
            self.status("Adjust the threshold sliders during the signal "
                       "step, then press this to use that value.")
            return
        working = self._working_imp
        if working is None or not fiji_io.is_open(working):
            self.status("The working image was closed.")
            return
        live = fiji_io.current_threshold(working)
        if live is None:
            self.status("No threshold is currently set on the image.")
            return
        low, high = live
        self._threshold = core.ThresholdResult(low, high, core.THRESH_MANUAL,
                                               overridden=True)
        self.status("Using threshold %.1f-%.1f for this signal (locked in "
                   "from the sliders)." % (low, high))
        self.refresh()

    def _accept_signal(self, working, box_roi):
        threshold = self._threshold
        if threshold is None:
            self.warn("No threshold is set. Re-arm the channel to recompute "
                      "one.")
            return

        selection = fiji_io.box_select(working, box_roi, threshold,
                                       min_area=self.s.min_area)
        if selection is None:
            self.warn("Nothing above threshold inside that box. Move the box, "
                      "or adjust the threshold sliders.")
            return

        working.setRoi(selection.roi)
        stats = fiji_io.measure(working, selection.roi)
        provenance = selection.provenance()
        provenance["MinArea"] = self.s.min_area

        pixels = fiji_io.roi_pixels(working, selection.roi)
        provenance["SaturatedFraction"] = core.saturated_fraction(
            pixels, fiji_io.value_max_for(working))

        name = self._channel
        self.s.captures[name] = Capture(
            selection.roi, self._armed_plane, stats,
            resolution=self._resolution, threshold=selection.threshold,
            provenance=provenance, box=box_roi.clone())
        self.s.skipped.discard(name)

        self._close_working_image()
        self._phase = PHASE_IDLE

        roi_names, image_path = self._export_channel(name)
        self._push_undo("signal for %s" % name, lambda: self._undo_capture(
            name, name, roi_names, image_path))
        self._after_channel()

    def _after_channel(self):
        resolved, total = self.s.progress()
        if resolved >= total:
            self.status("All %d/%d channels resolved. Space or Enter saves "
                        "this fish." % (resolved, total))
        else:
            self.status("%d/%d resolved. Pick the next channel."
                        % (resolved, total))

    # -- per-channel export -------------------------------------------------
    #
    # Each accepted eye/background/signal is written to the ROI archive and
    # (for a completed channel) an audit PNG immediately, not batched up for
    # fish-commit time. A crash between accepts used to lose every ROI and
    # image for a fish that had not yet been fully committed, even if every
    # channel but one had already been captured; now at most the channel that
    # was still in progress is at risk.

    def _fish_stub(self):
        session = self.s
        return "%s_%s" % (session.paths.session_name,
                          session.fish_label().replace(" ", ""))

    def _export_eye(self):
        """Returns (roi_names, image_path) for undo to clean up later."""
        session = self.s
        eye = session.captures.get("Eye")
        if eye is None:
            return [], None
        stub = self._fish_stub()
        name = stub + "__Eye"
        self.archive.add(eye.roi, name, eye.plane)
        self.archive.flush()
        path = _join(session.paths.image_dir, stub + "_Eye.png")
        fiji_io.export_audit_image(eye.plane, [(eye.roi, fiji_io.EYE_COLOR)],
                                   path)
        return [name], path

    def _export_background_only(self, name):
        """Archives the BG roi immediately for crash safety. The combined
        audit image for this channel (signal + box + background + threshold)
        is exported once the signal is accepted -- that is the first point at
        which all of those pieces exist together."""
        session = self.s
        background = session.backgrounds.get(name)
        if background is None:
            return [], None
        stub = self._fish_stub()
        qualified = "%s__BG_%s" % (stub, name)
        self.archive.add(background.roi, qualified, background.plane)
        self.archive.flush()
        return [qualified], None

    def _export_channel(self, name):
        """Returns (roi_names, image_path) for undo to clean up later."""
        session = self.s
        signal = session.captures.get(name)
        if signal is None:
            return [], None
        stub = self._fish_stub()
        names = ["%s__%s" % (stub, name)]
        self.archive.add(signal.roi, names[0], signal.plane)
        if signal.box is not None:
            # The box is saved too: it is the gesture that produced the
            # selection, so keeping it makes the segmentation auditable.
            names.append("%s__%s_box" % (stub, name))
            self.archive.add(signal.box, names[1], signal.plane)
        self.archive.flush()

        pairs = [(signal.roi, fiji_io.SIGNAL_COLOR)]
        if signal.box is not None:
            pairs.append((signal.box, fiji_io.BOX_COLOR))
        background = session.backgrounds.get(name)
        if background is not None:
            pairs.append((background.roi, fiji_io.BACKGROUND_COLOR))
        path = _join(session.paths.image_dir, "%s_%s.png" % (stub, name))
        fiji_io.export_audit_image(signal.plane, pairs, path)
        return names, path

    # -- undo (within the current fish) --------------------------------------

    def _push_undo(self, label, callback):
        self._fish_undo.append((label, callback))

    def _undo_capture(self, key, channel_name, roi_names, image_path):
        """Reverses an accepted eye or signal: un-capture it, and remove
        whatever _export_eye/_export_channel just wrote to disk."""
        self.s.captures.pop(key, None)
        self.s.skipped.discard(channel_name)
        self.archive.remove_named(roi_names)
        _remove_file(image_path)
        self.status("Undid: %s is no longer captured. Press its channel key "
                    "to redo." % channel_name)

    def _undo_background(self, name, roi_names):
        self.s.backgrounds.pop(name, None)
        self.s.skipped.discard(name)
        self.archive.remove_named(roi_names)
        self.status("Undid: %s background is cleared. Press its channel key "
                    "to redo." % name)

    def _undo_skip(self, name):
        self.s.skipped.discard(name)
        self.status("Undid: %s is no longer marked skipped." % name)

    def undo(self):
        """The one Undo action: reverses whatever just happened.

        Pops the current fish's own action stack if it has anything --
        undoing an accepted eye/background/signal or a skip is cheap and
        touches nothing on disk that a *later* action didn't just write, so it
        needs no confirmation. Only once that stack is empty does this fall
        through to undoing an entire previously SAVED fish, which does touch
        the on-disk CSV/archive and is confirmed by the panel before calling
        this.
        """
        self._close_working_image()
        self._phase = PHASE_IDLE
        self._channel = None

        if self._fish_undo:
            label, callback = self._fish_undo.pop()
            callback()
            self.refresh()
            return

        self._undo_last_fish()

    # -- skipping ---------------------------------------------------------

    def skip(self):
        """S. Record a deliberate skip, distinct from 'not done yet'."""
        session = self.s
        name = self._channel
        if name is None:
            self.status("Nothing armed to skip.")
            return

        if self._phase == PHASE_SIGNAL:
            # Background is already measured; drop it too, since a background
            # with no signal has nothing to correct.
            session.backgrounds.pop(name, None)

        key = "Eye" if name == session.bf_name else name
        session.captures.pop(key, None)
        session.backgrounds.pop(name, None)
        session.skipped.add(name)

        self._close_working_image()
        self._phase = PHASE_IDLE
        self._channel = None
        self._armed_plane = None

        self._push_undo("skip of %s" % name, lambda: self._undo_skip(name))
        self.status("Skipped %s. It will be recorded as skipped, not blank."
                    % name)
        self.refresh()

    # -- overrides --------------------------------------------------------

    def use_current_plane(self):
        """Redirect the armed channel to wherever the operator is actually
        looking on the LIVE stack, and rebuild the working copy from there."""
        session = self.s
        if self._channel is None:
            self.status("Arm a channel first, then redirect it.")
            return

        live = fiji_io.current_image()
        if live is None or fiji_io.is_working_image(live):
            self.status("Click your original image/stack (not the small "
                        "working window), then try again.")
            return

        plane = fiji_io.plane_of(live)
        resolution = session.manifest.override_plane(
            session.fish_ordinal, self._channel, plane)
        session.manifest.save(session.paths.manifest)

        expected_text = (resolution.expected.describe()
                         if resolution.expected else "unmapped")
        name = self._channel
        self._close_working_image()
        self._open_channel(name, live, resolution)
        self.status("%s now points at %s for this fish (was %s). Recorded as "
                    "an override." % (name, plane.describe(), expected_text))
        self.refresh()

    def relabel(self, from_channel, to_channel):
        session = self.s
        try:
            session.manifest.relabel(session.fish_ordinal, from_channel,
                                     to_channel)
        except ValueError as error:
            self.status(str(error))
            return
        session.manifest.save(session.paths.manifest)
        self.status("Swapped %s and %s for this fish."
                    % (from_channel, to_channel))
        self.refresh()

    def promote_override(self):
        session = self.s
        if self._channel is None:
            self.status("Arm the channel whose override you want to promote.")
            return
        try:
            changed = session.manifest.promote_override(
                session.fish_ordinal, self._channel,
                through_fish=session.fish_target)
        except ValueError as error:
            self.status(str(error))
            return
        session.manifest.save(session.paths.manifest)
        self.status("Applied the %s correction to %d later fish."
                    % (self._channel, len(changed)))
        self.refresh()

    # -- commit -----------------------------------------------------------

    def commit(self):
        """Save the fish. Runs off the EDT so Fiji does not freeze."""
        if self._committing:
            self.status("Already saving; give it a moment.")
            return
        if self.s.finished:
            self.status("Session finished.")
            return

        # Enter/"Save fish" can be pressed mid-draw. Whatever is on the working
        # copy right now was never accepted, so it is discarded, not saved --
        # same rule as always: only an accepted (Space-confirmed) ROI counts.
        self._close_working_image()
        self._phase = PHASE_IDLE
        self._channel = None

        self._committing = True
        self.status("Saving %s..." % self.s.fish_label())

        def work():
            try:
                self._commit_now()
            except Exception:
                self.status("Save failed; see the Log. Nothing was recorded.")
                fiji_io.log(traceback.format_exc())
            finally:
                self._committing = False
                self.refresh()

        thread = threading.Thread(target=work)
        thread.setDaemon(True)
        thread.start()

    def _commit_now(self):
        session = self.s
        row = self._build_row()

        # The journal first: it is the durable record, and everything else is
        # derived from it. A crash after this point loses no data.
        # ROIs and audit images were already written per-channel as each was
        # accepted (see _export_eye/_export_background_only/_export_channel),
        # so a crash mid-fish loses at most the row for whatever channel was
        # still in progress -- never anything already accepted. Only the CSV
        # row remains to be recorded here.
        roi_names = self._archive_names()
        seq = session.journal.record_commit(
            row, roi_names=roi_names,
            context={"fish": session.fish_ordinal,
                     "overrides": session.manifest.override_count()})

        journal_mod.rebuild_csv(session.journal, session.paths.csv,
                                core.csv_header(session.fl_names))

        session.reset_fish()
        session.fish_ordinal = session.journal.next_sequence()
        self._fish_undo = []

        committed = session.committed_count()
        if committed >= session.fish_target:
            self.status("Target reached: %d fish saved. Add another, or finish."
                        % committed)
            if self.panel is not None:
                self.panel.prompt_target_reached()
        else:
            self.status("Saved (seq %d). %d of %d fish done."
                        % (seq, committed, session.fish_target))

    def _build_row(self):
        session = self.s
        eye = session.captures.get("Eye")

        image_info = {"Operator": session.operator,
                      "MeasuredAt": journal_mod._timestamp()}
        if eye is not None:
            image_info.update(eye.provenance)
            imp = fiji_io.image_by_key(eye.plane.image_key) if eye.plane else None
            if imp is not None:
                image_info.update(fiji_io.calibration_info(imp))
            if eye.resolution is not None:
                image_info.update(eye.resolution.as_row_fields())

        channel_results = {}
        for name in session.fl_names:
            signal = session.captures.get(name)
            background = session.backgrounds.get(name)
            if name in session.skipped:
                channel_results[name] = {"signal": core.SKIPPED,
                                         "background": core.SKIPPED}
                continue
            channel_results[name] = {
                "signal": signal.stats if signal else None,
                "background": background.stats if background else None,
                "threshold": signal.threshold if signal else None,
                "provenance": signal.provenance if signal else None,
            }

        eye_stats = core.SKIPPED if session.bf_name in session.skipped else (
            eye.stats if eye else None)

        label = ""
        if eye is not None and eye.plane is not None:
            label = eye.plane.image_title or ""

        return core.build_row(label, session.fish_label(), session.fl_names,
                              eye_stats=eye_stats,
                              channel_results=channel_results,
                              image_info=image_info)

    def _archive_names(self):
        """Every qualified ROI name this fish's currently captured channels
        WOULD have in the archive. Used to tell the journal what to remove if
        this fish is later undone -- the ROIs themselves were already written
        per-channel (see _export_eye/_export_background_only/_export_channel),
        this just has to name them the same way."""
        session = self.s
        stub = self._fish_stub()
        names = []
        if "Eye" in session.captures:
            names.append(stub + "__Eye")
        for name in session.fl_names:
            if name in session.captures:
                names.append("%s__%s" % (stub, name))
                names.append("%s__%s_box" % (stub, name))
            if name in session.backgrounds:
                names.append("%s__BG_%s" % (stub, name))
        return names

    # -- undo -------------------------------------------------------------

    def _undo_last_fish(self):
        """Withdraw the last SAVED fish. The fallback undo() reaches for once
        the current fish has no smaller in-progress action left to undo.

        Appends a tombstone; nothing is deleted. Rows written by earlier runs of
        this session are untouched, which is the failure the legacy rewrite-from-
        memory undo caused.
        """
        session = self.s
        last = session.journal.last_live_commit()
        if last is None:
            self.status("Nothing to undo.")
            return

        session.journal.record_undo(last["seq"])
        journal_mod.rebuild_csv(session.journal, session.paths.csv,
                                core.csv_header(session.fl_names))
        self.archive.remove_named(last.get("roi_names", []))

        session.reset_fish()
        session.fish_ordinal = session.journal.next_sequence()
        session.finished = False
        self._fish_undo = []
        self.status("Withdrew %s. It stays in the journal as a tombstone; "
                    "redraw it as %s." % (last.get("row", {}).get("FishID", "?"),
                                          session.fish_label()))
        self.refresh()

    def add_fish(self):
        self.s.fish_target += 1
        self.s.finished = False
        self.status("Target is now %d fish." % self.s.fish_target)
        self.refresh()

    def finish(self):
        self.s.finished = True
        self.status("Session complete: %d fish saved. CSV: %s"
                    % (self.s.committed_count(), self.s.paths.csv))
        self.refresh()

    def shutdown(self):
        """Called when the panel closes, so a working image never lingers."""
        self._close_working_image()


def _whole_image_roi(imp):
    from ij.gui import Roi as _Roi
    return _Roi(0, 0, imp.getWidth(), imp.getHeight())


def _join(directory, name):
    return os.path.join(directory, name)


def _remove_file(path):
    """Best-effort delete for an undo cleanup; never worth failing over."""
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
