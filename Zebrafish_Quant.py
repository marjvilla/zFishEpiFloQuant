#
# Zebrafish_Quant.py
# High-throughput zebrafish fluorescence quantification for Fiji / ImageJ.
#
# Installed via install.sh (see README.md), which places ONLY this file
# under Fiji's scripts/Plugins/ZebrafishQuant/ and puts the zfquant/ package
# under <Fiji root>/jars/Lib/zfquant instead. That split matters: Fiji's
# script menu recurses into every folder under scripts/Plugins and turns
# each .py file it finds into its own clickable menu entry -- if zfquant/
# sat next to this file, every internal module (core.py, fiji_io.py, ...)
# would show up as a bogus, individually-clickable submenu item. jars/Lib is
# on Jython's import path but is never scanned for menu items, so only this
# one entry point appears in the menu.
#
#   Corrected Intensity = FL IntDen - (Mean BG x FL Area)
#
# This is a thin entry point. The work lives in the zfquant package:
#
#   zfquant/core.py      measurement + threshold math   (pure, unit-tested)
#   zfquant/manifest.py  fish -> plane mapping          (pure, unit-tested)
#   zfquant/journal.py   append-only session state      (pure, unit-tested)
#   zfquant/fiji_io.py   every ImageJ call
#   zfquant/workflow.py  the per-fish state machine
#   zfquant/ui.py        the control panel
#   zfquant/startup.py   setup dialogs
#   zfquant/review.py    the interactive setup-time review that builds the plan
#
# Run the pure-side tests on any machine with Python 3, no install needed:
#
#   python3 -m unittest discover -s tests -v
#
# Target: Fiji (ImageJ2), Jython 2.7 (Python 2 syntax).

from __future__ import division

import os
import sys
import traceback

from ij import IJ


def _ensure_package_importable():
    """Let `import zfquant` work regardless of exactly where this file and
    the zfquant/ package end up.

    Tries the script's own directory first (works if zfquant/ happens to sit
    right next to this file, e.g. when just running it ad hoc from the
    Script Editor). Then computes <Fiji root>/jars/Lib -- where install.sh
    actually puts zfquant/ -- by walking up from this file's location to the
    "scripts" folder and looking one level up. Fiji's script runner does not
    reliably put either location on sys.path itself, and the failure mode is
    a bare ImportError with no hint as to why.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [here]

    parts = here.split(os.sep)
    if "scripts" in parts:
        fiji_root = os.sep.join(parts[:parts.index("scripts")])
        candidates.append(os.path.join(fiji_root, "jars", "Lib"))

    for path in candidates:
        if path and path not in sys.path:
            sys.path.insert(0, path)


def main():
    _ensure_package_importable()

    from zfquant import fiji_io, journal, manifest as manifest_mod
    from zfquant import review, startup, ui, workflow

    config = startup.prompt()
    if config is None:
        IJ.log("[ZFQuant] Setup cancelled.")
        return

    paths = journal.SessionPaths(config.output_root, config.session_name)
    decision = startup.ask_about_existing_session(paths)
    if decision == startup.RESUME_CANCEL:
        IJ.log("[ZFQuant] Cancelled at the existing-session prompt.")
        return
    if decision == startup.RESUME_NEW and paths.exists():
        IJ.error("Run setup again with a different session name.")
        return

    images = fiji_io.open_images()
    resuming = (decision == startup.RESUME_RESUME)

    manifest = None
    if resuming:
        # Reload the plan from the earlier run, including any per-fish
        # overrides it accumulated -- rebuilding fresh here would silently
        # discard them. Only fall back to a fresh review if the saved
        # manifest is missing or corrupt (a session with a journal but no
        # usable manifest.json), rather than crashing outright.
        try:
            manifest = manifest_mod.Manifest.load(paths.manifest)
        except Exception:
            IJ.log("[ZFQuant] Saved plan could not be loaded, running a "
                   "fresh review instead: " + traceback.format_exc())
            resuming = False

    if manifest is None:
        manifest = review.run_review(config.layout, config, images)
        if manifest is None:
            IJ.log("[ZFQuant] Review cancelled; nothing was created.")
            return

    config.fish_total = max(1, manifest.fish_count())

    paths.ensure()
    if not resuming:
        manifest.save(paths.manifest)

    session_journal = journal.SessionJournal(paths.journal)
    if session_journal.corrupt_lines:
        IJ.log("[ZFQuant] Skipped %d unreadable journal line(s) from a "
               "previous crash." % session_journal.corrupt_lines)
    session_journal.start_session(config.session_name, config.channel_names,
                                  config.bf_name, operator=config.operator,
                                  extra={"layout": config.layout,
                                         "k": config.k,
                                         "min_area": config.min_area})

    session = workflow.Session(config, paths, manifest, session_journal)
    controller = workflow.Controller(session)
    ui.ControlPanel(controller)

    resumed = session.committed_count()
    if resumed:
        controller.status("Resumed '%s': %d fish already saved. Next is %s."
                          % (config.session_name, resumed,
                             session.fish_label()))
    else:
        controller.status("Ready. Pick a channel to begin. Background box "
                          "first, then the signal box.")


try:
    main()
except Exception:
    IJ.log(traceback.format_exc())
    IJ.error("Zebrafish Quant failed to start:\n\n" + traceback.format_exc())
