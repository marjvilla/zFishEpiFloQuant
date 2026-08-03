# Zebrafish Quantification Tool — Overview for a Rebuild

## What this program is for

A wet-lab biologist quantifies fluorescent signal (e.g. GFP, RFP) in an area of zebrafish, using brightfield (BF) to outline the eye and
fluorescence channels to measure signal. For each fish they need, per
fluorescent channel:

```
Corrected Intensity = FL Integrated Density - (Mean Background Intensity * FL Area)
```

plus the eye area (for normalization). This has to happen for many fish,
fast, inside Fiji/ImageJ, without the user leaving the mouse/keyboard to run
separate menu commands for every step (draw ROI → measure → write to
spreadsheet → repeat).

This document describes what was actually built (a single Jython/Fiji
script), the hard-won lessons from real usage, and what a rebuild should do
differently. **The current script works, but it accreted complexity through
many rounds of "actually, my data doesn't look like that" — a rebuild should
absorb those lessons up front instead of discovering them one bug report at
a time.**

---

## The core domain confusion a rebuild must resolve FIRST

The single biggest source of bugs in this project was ambiguity about **how
image data is organized**, because the user's real data didn't match the
initial assumptions. There are at least three distinct layouts that all call
themselves "a stack," and they need fundamentally different navigation logic:

1. **True hyperstack** — one image with a real channel dimension
   (`getNChannels() > 1`), possibly also Z and T dimensions. Switching
   "channel" means `imp.setPosition(c, z, t)`. This is the only layout where
   automatic channel-switching is *safe* to do programmatically.
2. **One big plain stack holding EVERYTHING** — every fish and every channel
   as slices in a single plain stack (`getNChannels() == 1`, many slices).
   There is no fixed "slice N = channel X" rule, because slice number depends
   on which fish you're on. **Auto-navigating to a static slice index is
   flatly wrong here** — it was the single biggest recurring bug in this
   project. The tool must never move the slice on this user's behalf; it
   only arms the tool and reads whatever slice the human has already
   scrolled to.
3. **One separate plain image per channel** — e.g. three separate open
   windows, one per channel, no shared stack at all. The user manually
   selects which window is "active" and the tool must always operate on
   *whichever image is currently frontmost in the host application*, never
   on some internally cached "last known" image.

A rebuild should ask the user up front which layout they have (a single
explicit setup question), rather than trying to auto-detect it, because
auto-detection based on `getNChannels()` cannot distinguish layout 2 from
layout 3, and guessing wrong silently corrupts measurements (it looks like
it worked — the image updates, a value gets written — but it's the wrong
plane's data).

**Recommendation:** make "how are your channels organized?" one of the very
first setup questions, with the three options above spelled out in plain
language and a one-line consequence for each ("the tool will never touch
your stack position" / "the tool will switch channels for you" / "you pick
the window, the tool follows").

---

## Feature list (what the tool actually does)

### Setup (once per session)
- Ask for: a session/base name, up to 4 channel names in stack order (e.g.
  BF, RFP, GFP), which one is brightfield (auto-inferred from the name if
  something is literally called "BF"/"Brightfield"/"DIC"/etc., only prompted
  explicitly if ambiguous), starting mode (guided vs free-form — see below),
  whether to work on just the currently-active image or all open images, and
  a **total number of fish to quantify for the whole session** (not per
  image — this was a repeated point of confusion; the counter is global).
- Blank channel name slots are simply skipped — they never become
  selectable, so there's no way to accidentally pick a channel that doesn't
  exist.
- Output goes to `[chosen folder]/[session name]/`.

### Two operating modes, but really one shared engine
- **Wizard (semi-automated):** for a true hyperstack. Automatically switches
  to BF and arms an ellipse tool, waits for the user, then automatically
  switches to each fluorescent channel, applies a threshold, arms the magic
  wand, then automatically arms a rectangle for the background box. The user
  presses Space to accept each step and move to the next; Backspace goes
  back one step (channel or eye) without losing already-captured data;
  pressing a channel hotkey/button at any time interrupts the sequence and
  does that channel directly (no separate "switch to manual" button needed —
  it just works, because arming a channel is the same underlying action in
  both modes).
- **Manual/hotkey mode:** for the "separate window per channel" and "one big
  combined stack" layouts, where the tool cannot safely auto-navigate. The
  user clicks into whatever image/plane they want, then presses a hotkey or
  clicks a per-channel button in the floating panel to arm that channel
  (ellipse for BF, wand+threshold for fluorescence) on **whatever image is
  currently active right now** — never a cached reference. A single stack
  with no real channel dimension automatically starts in this mode.
- Both modes share one underlying state machine: an ROI is captured only
  when the user explicitly presses Space (never on wand-click completion —
  early versions tried to auto-capture on ROI completion and it made
  re-thresholding/redrawing impossible), and `S` skips the current
  channel/step without recording anything (e.g. eye not visible, channel not
  present for this fish).

### Per-fish workflow
- **Eye (BF):** ellipse tool armed, no threshold shown (threshold is cleared
  whenever BF is active, so it never bleeds over from a fluorescent step).
- **Each fluorescent channel:** a threshold overlay is shown so the user can
  wand-click just the signal, immediately followed by a rectangle tool for a
  paired background box (threshold is cleared before drawing the background,
  and Brightness/Contrast display settings are explicitly snapshotted and
  restored around all threshold operations so they never drift).
- A live "channels done: X/Y" indicator plus a `[done]` marker on each
  channel's button, updated after every accept/skip.
- Once every channel is captured, pressing Space with nothing pending just
  commits the fish (no extra confirmation needed).
- **"Add Another Fish"** commits the current data and starts a new fish on
  the *same* image (for hyperstacks where a T/frame dimension holds one fish
  per frame, this automatically advances to the next frame — this was
  missed in an early version and silently kept re-measuring frame 1 for
  every "fish").
- **"Skip This Fish"** reduces the remaining target by one and discards
  in-progress work, for when the user configured more fish than they
  actually have.
- **"Go Back / Modify Previous"** pops the last committed fish: removes its
  row from the CSV, removes its named entries from the shared ROI archive,
  re-selects its image (and, for hyperstacks, its exact frame), and restores
  its *exact* ROI objects from an in-memory snapshot taken at commit time —
  deliberately not re-parsed from any file, which is far more reliable than
  trying to reconstruct position from a saved ROI's metadata.
- **Reaching the fish target** pops an explicit Yes/No dialog ("add one
  more, or finish?") rather than silently either stopping or letting the
  user keep going with stale, uncleared state — an earlier version let
  "Add Another Fish" be pressed after the session was already "done," which
  corrupted data because per-fish state hadn't been reset.

### The "don't silently measure the wrong thing" safety net
For the single-big-stack layout, nothing stops a user from scrolling the
stack with the mouse wheel while a channel is armed. The tool records the
exact slice it was armed on and, if the user tries to accept (Space) after
the stack has drifted to a different slice, it **refuses** and tells them
exactly which slice to return to, rather than silently measuring the wrong
plane. A true OS-level "lock" on the scrollbar isn't realistically
achievable from a Jython plugin script, so this is a deliberate
detect-and-refuse compromise rather than a hard lock.

### Data captured (be exhaustive — this was explicitly requested)
Per ROI (eye, each channel's signal, each channel's background), every
standard statistic is saved, not just the ones needed for the corrected-
intensity formula: Area, Mean, StdDev, Median, Mode, Min, Max, Perimeter,
IntDen (calibrated integrated density = Area×Mean), RawIntDen (uncalibrated
= raw pixel count × Mean — this is a *different* number from IntDen if the
image has spatial calibration, and both matter for downstream analysis),
Skewness, Kurtosis. Plus the one derived value, Corrected Intensity, per
fluorescent channel. The idea: capture everything now, decide what's
actually useful during analysis later — don't force the user to re-open
images because a column was left out.

### Outputs
- One CSV for the whole session (`[session]_dataset.csv`), header generated
  from a single shared list of stat names so the header and the row-building
  code can't drift apart.
- **One combined ROI archive for the whole session**
  (`[session]_all_ROIs.zip`), not one file per fish — every fish's ROIs are
  added to a single accumulating RoiManager list with fish-qualified names
  (`<image>_Fish<N>__<label>`) so nothing collides, and it's re-saved after
  every commit. (An earlier version created a new zip file per fish; the
  user correctly pushed back that this was needless clutter.)
- Presentation JPEGs per fish: the BF image with just the eye outline, and
  one JPEG per fluorescent channel showing both its signal ROI and its
  paired background ROI outlined together (for auditing background choices)
  — each rendered on the *exact* image/frame that ROI came from, not
  whatever happens to be on screen at export time.

---

## Architecture as built (for reference, not necessarily to copy)

Single Jython file, roughly these responsibilities:

- `StartupConfig` — the one-time setup dialog(s) and channel-name→BF
  inference.
- `ZebrafishSession` — the state machine: current image, current fish
  number (global, not per-image), captured ROIs for the in-progress fish
  (each stored with the *exact* image + channel + Z + T it was drawn on,
  never just "whatever's on screen"), history for undo, CSV/output paths.
- `QuantEngine` — turns captured ROIs into a measurement row, always
  re-navigating to each ROI's own recorded position before measuring (so
  measuring channel A doesn't require channel A to still be on screen).
- `Exporter` — ROI archive + presentation JPEGs.
- `WorkflowController` — the actual per-key/per-button behavior; the
  largest class, because it's where "wizard step" and "direct channel
  action" logic both live and had to be reconciled (a channel button press
  always wins over wherever the wizard's own cursor happens to be).
- `ControlPanel` — a small floating, always-on-top, non-modal Swing window:
  mode/progress/lock-warning labels, one button per channel (rebuilt live
  whenever the channel list changes), and the action buttons (accept/back/
  skip/commit/add-fish/skip-fish/go-back/hop-image).

### Two functions that look similar but must stay separate
`arm_to_channel()` (only moves the stack position for a real hyperstack;
does nothing on a plain stack — this is what a channel hotkey/button calls)
vs. `set_channel()` (always re-navigates, including plain-stack slices —
this is only for re-measuring/re-exporting an already-recorded ROI at its
exact saved position). Collapsing these into one function was the source of
the recurring "why did it jump to a different slice/image" bugs; a rebuild
should keep this distinction explicit and well-named from day one, e.g.
`arm_channel_for_drawing()` vs. `seek_to_recorded_position()`.

---

## Concrete bugs hit during development (so a rebuild can avoid them)

1. **A Jython/Java string-encoding trap:** any non-ASCII character (even a
   simple ✓ checkmark) in a string that reaches a Swing component can throw
   `UnicodeDecodeError` under Jython 2.7's default ASCII codec, even with a
   UTF-8 source-encoding header. Use plain ASCII (`[done]`, not `✓`) for any
   text that ends up in a JLabel/JButton.
2. **`IJ.run(imp, "Set JPEG Quality...", ...)` is not a real headless-safe
   command** — it pops a dialog and can throw `RuntimeException: Macro
   canceled`. Use the static `FileSaver.setJpegQuality(int)` instead.
3. **Fiji's own "Window" menu binds plain digit keys 1–9** to switch between
   open image windows. If a custom `KeyListener` doesn't call
   `event.consume()`, a digit-key channel hotkey both does its own thing
   *and* triggers Fiji's window-switch — looks exactly like "pressing 2
   jumps to the 2nd image." Always consume handled key events.
4. **`Toolbar.getInstance().WAND`** (calling a static field through an
   instance obtained via `getInstance()`) can NPE if the toolbar isn't fully
   realized yet at script-load time. Use the class constant directly
   (`Toolbar.WAND`).
5. **Auto-threshold polarity matters enormously.** `IJ.setAutoThreshold(imp,
   "Default")` (no "dark") assumes a bright background — on fluorescence
   images (bright signal, dark background) it selects the *background*
   instead of the signal, which looks like "thresholds 0–95% of the image."
   Either use `"Default dark"`, or better, compute a direct percentage-of-
   range cutoff (this project ended up doing the latter: threshold = top 5%
   of the current slice's own min–max range, which is more predictable than
   any histogram-shape-dependent auto method and can't silently produce a
   degenerate/invisible threshold).
6. **ImageJ's interactive Threshold window keeps its own state and can
   silently override a threshold you just set programmatically** the moment
   the image/position changes underneath it. If you need a specific,
   non-default threshold value, apply it *after* opening/refocusing the
   Threshold window, not before.
7. **Never auto-clear a live UI panel (like the ROI Manager) right after
   populating it.** An early version reset the RoiManager immediately after
   saving to it, so the panel looked empty a split-second after every
   commit — data was fine on disk, but it looked broken to the user.
8. **Don't silently drop state when a "session complete" condition is hit.**
   Reaching a target count and then allowing further actions without
   resetting per-fish state is a data-corruption bug waiting to happen; force
   an explicit choice (continue vs. stop) at that boundary.

---

## What a rebuild should seriously consider doing differently

- **Ask about data layout explicitly at setup**, as described above, instead
  of inferring it from `getNChannels()`. This alone would have prevented
  most of the back-and-forth in this project.
- **Make "arm vs. measure" position-handling one obviously-named pair of
  functions from the start**, with a code comment explaining why they must
  never be merged.
- **Treat "fish count" as global from the first draft**, not "per image" —
  retrofitting this required touching commit/rollback/UI code in several
  places.
- **Consider whether the floating panel + hotkeys is the right interaction
  model at all**, or whether a simpler, single always-visible checklist per
  fish (BF ✓ / RFP ✓ / GFP ✓, each row clickable to arm it) would need fewer
  words of explanation than "press 1/2/3, then Space, then watch for the
  red LOCKED banner." The current design grew hotkey-first because that's
  what was asked for iteratively; a from-scratch design aimed at "easier to
  use" might reasonably lead with buttons/clicks and treat hotkeys as an
  accelerator layer on top, not the primary mental model.
- **Surface the slice-lock concept differently.** A detect-and-refuse
  safety net is a workaround for not being able to truly freeze the host
  application's own navigation from a script. If the rebuild has any way to
  actually disable stack scrolling while a channel is armed (even
  temporarily disabling the stack scrollbar widget), that would be strictly
  better UX than "you did it wrong, please undo your scroll and try again."
- **One combined output file per artifact type per session** (one CSV, one
  ROI archive) was the right call once discovered — build that in from the
  start rather than per-fish files that need consolidating later.
- **Save the full standard measurement set from the first version**, not
  just the values needed for one formula — re-deriving "oh, we also need
  RawIntDen" after the fact meant touching the CSV header, the row builder,
  and re-explaining the naming convention.
