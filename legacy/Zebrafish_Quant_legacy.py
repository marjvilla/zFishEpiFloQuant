#
# Zebrafish_Quant.py
# High-throughput zebrafish fluorescence quantification for Fiji / ImageJ.
#
# A dual-mode acquisition tool:
#   * Semi-Automated Wizard  -- guided, step-by-step tool/channel arming.
#   * Interactive Hotkey Mode -- free-form, key-driven, per-fish toggle.
#
# The two modes share one state machine (ZebrafishSession) and one floating,
# non-modal control panel (ControlPanel). Either can be switched to at any time,
# per fish, without losing progress.
#
# Author: Bioimage Informatics
# Target: Fiji (ImageJ2), Jython 2.7 (Python 2 syntax).
#
# ---------------------------------------------------------------------------
# Math per fluorescent channel, per fish:
#   Corrected Intensity = FL IntegratedDensity - (MeanBG * FL Area)
# The eye ROI area is stored verbatim in its own column for normalization.
# ---------------------------------------------------------------------------

import os
import csv
import traceback

from java.awt import Color, GridLayout, BorderLayout, Dimension, FlowLayout, Font
from java.awt.event import KeyEvent, KeyAdapter, WindowAdapter
from javax.swing import (JFrame, JPanel, JButton, JLabel, JTextField, JComboBox,
                         JCheckBox, BoxLayout, BorderFactory, SwingUtilities,
                         JScrollPane, JFileChooser, JOptionPane)
from javax.swing.border import EmptyBorder

from ij import IJ, ImagePlus, ImageStack, WindowManager
from ij.gui import Toolbar, Roi, Overlay, GenericDialog, WaitForUserDialog
from ij.gui import RoiListener
from ij.process import ImageStatistics, ImageConverter
from ij.measure import Measurements, ResultsTable
from ij.plugin.frame import RoiManager
from ij.io import FileSaver


# ===========================================================================
#  Constants
# ===========================================================================

TOOL_RECT = Toolbar.RECTANGLE
TOOL_OVAL = Toolbar.OVAL
TOOL_WAND = Toolbar.WAND

MODE_WIZARD = "Semi-Automated Wizard"
MODE_MANUAL = "Interactive Hotkey Mode"

# Wizard step identifiers.
STEP_EYE = "eye"
STEP_FL = "fl"          # arm wand on a fluorescent channel
STEP_BG = "bg"          # draw background rectangle for the current channel
STEP_DONE = "done"

# Direct percentage-of-range threshold: only the brightest
# (1 - THRESHOLD_PERCENT) fraction of each slice's own intensity range is
# selected as signal (0.95 -> keep the top 5%, i.e. "0-95%" is background).
THRESHOLD_PERCENT = 0.95

# JPEG quality knob (FileSaver reads this ImageJ pref).
JPEG_QUALITY = 70


# ===========================================================================
#  Utility helpers
# ===========================================================================

def ensure_dir(path):
    """Create a directory (and parents) if it does not already exist."""
    if path and not os.path.isdir(path):
        os.makedirs(path)


def log(msg):
    IJ.log("[ZFQuant] " + str(msg))


def arm_tool(tool_id):
    """Select an ImageJ toolbar tool by numeric id."""
    Toolbar.getInstance().setTool(tool_id)


def set_channel(imp, channel_index_1based, z=None, t=None):
    """Move to a given (1-based) position: channel dimension for a true
    hyperstack, or slice for a plain stack.

    This is for RE-navigating to an exact, previously-recorded position (used
    when measuring/exporting a stored ROI record) -- NOT for arming a channel
    action. Use arm_to_channel() for that; see its docstring for why they
    differ.

    For a hyperstack, z/t default to the image's CURRENT z/t if not given,
    but callers that recorded the original z/t (store_roi() does) should
    pass them through so the exact original plane is re-measured.
    """
    if imp is None:
        return
    nc = imp.getNChannels()
    if nc > 1:
        c = max(1, min(channel_index_1based, nc))
        zz = z if z is not None else imp.getZ()
        tt = t if t is not None else imp.getT()
        imp.setPosition(c, zz, tt)
    elif imp.getStackSize() > 1:
        z2 = max(1, min(channel_index_1based, imp.getStackSize()))
        imp.setSlice(z2)
    imp.updateAndDraw()


def arm_to_channel(imp, channel_index_1based):
    """Move to a channel when ARMING a channel action (hotkey/button/wizard
    step). Only navigates for a true hyperstack (getNChannels() > 1).

    Plain stacks are deliberately left untouched: a single big stack may hold
    many fish AND channels together with no fixed per-channel slice number
    (e.g. fish 5's RFP is not always slice 2), so jumping to a static
    configured index would land on the wrong fish's plane. The user
    navigates plain stacks themselves; arming just readies the tool on
    whatever slice is already showing.
    """
    if imp is None:
        return
    nc = imp.getNChannels()
    if nc > 1:
        c = max(1, min(channel_index_1based, nc))
        imp.setPosition(c, imp.getZ(), imp.getT())
        imp.updateAndDraw()


def measure_roi(imp, roi):
    """Return an ImageStatistics for `roi` on the *currently displayed* slice of imp."""
    ip = imp.getProcessor()
    ip.setRoi(roi)
    measures = (Measurements.AREA | Measurements.MEAN | Measurements.STD_DEV |
                Measurements.INTEGRATED_DENSITY | Measurements.MIN_MAX |
                Measurements.MEDIAN | Measurements.MODE |
                Measurements.SKEWNESS | Measurements.KURTOSIS)
    stats = ImageStatistics.getStatistics(ip, measures, imp.getCalibration())
    return stats


# Every raw stat saved per ROI (eye, each FL signal, each background). Keep
# this list and QuantEngine._full_stats() in lock-step.
STAT_KEYS = ["Area", "Mean", "StdDev", "Median", "Mode", "Min", "Max",
            "Perimeter", "IntDen", "RawIntDen", "Skewness", "Kurtosis"]



# ===========================================================================
#  Startup configuration UI
# ===========================================================================

class StartupConfig(object):
    """Modal dialog collecting channels, the output folder, and starting mode.

    The tool operates on images you have ALREADY opened in Fiji (drag-and-drop
    your prepared hyperstacks / stacks first), so no input folder is needed.
    """

    def __init__(self):
        # channels: list of {"index": stack-position (1-based), "name": str}.
        # `index` is the real slice/channel position, preserved even if some
        # slots are left blank, so the mapping to the image never drifts.
        self.channels = []
        self.bf_index = 1             # stack position of the brightfield channel
        self.output_dir = None
        self.session_name = "Experiment"
        self.mode = MODE_WIZARD
        self.active_only = True       # process only the active image vs. all open
        self.fish_total = 1           # TOTAL fish across the whole session
        self.ok = False

    def prompt(self):
        n_open = 0
        ids = WindowManager.getIDList()
        if ids is not None:
            n_open = len(ids)
        if n_open == 0:
            IJ.error("No images are open.\nOpen your stacks in Fiji, then re-run.")
            return False

        # --- Dialog 1: names, session, mode, counts. ---
        gd = GenericDialog("Zebrafish Quantification - Setup")
        gd.addMessage("Operating on %d image(s) already open in Fiji." % n_open)
        gd.addStringField("Session / base name:", "Experiment", 20)
        gd.addMessage("Channel names in stack order "
                      "(leave a slot blank to skip it):")
        defaults = ["BF", "RFP", "GFP", ""]
        for i in range(4):
            gd.addStringField("Channel %d name:" % (i + 1), defaults[i], 12)
        gd.addChoice("Starting mode:", [MODE_WIZARD, MODE_MANUAL], MODE_WIZARD)
        gd.addCheckbox("Process only the ACTIVE image (else all open images)",
                       True)
        gd.addNumericField("Total number of fish to quantify (this session):", 1, 0)
        gd.showDialog()
        if gd.wasCanceled():
            return False

        self.session_name = (gd.getNextString().strip() or "Experiment")
        names = [gd.getNextString().strip() for _ in range(4)]
        self.mode = gd.getNextChoice()
        self.active_only = gd.getNextBoolean()
        self.fish_total = max(1, int(gd.getNextNumber()))

        # Only non-blank slots become real channels; keep their true positions.
        self.channels = [{"index": i + 1, "name": names[i]}
                         for i in range(4) if names[i] != ""]
        if not self.channels:
            IJ.error("At least one channel name is required.")
            return False

        # --- Brightfield channel: infer it from the name if possible, and
        # only bother the user with a picker dialog when it's genuinely
        # ambiguous (e.g. nothing is named "BF"/"Brightfield"/"DIC").
        ch_names = [c["name"] for c in self.channels]
        bf_name = self._infer_bf_name(ch_names)
        if bf_name is None:
            gd2 = GenericDialog("Brightfield Channel")
            gd2.addChoice("Which channel is Brightfield (eye)?", ch_names, ch_names[0])
            gd2.showDialog()
            if gd2.wasCanceled():
                return False
            bf_name = gd2.getNextChoice()
        self.bf_index = next(c["index"] for c in self.channels
                             if c["name"] == bf_name)

        self.output_dir = self._choose_dir("Select OUTPUT folder")
        if not self.output_dir:
            return False

        self.ok = True
        return True

    def fl_channels(self):
        """Fluorescent channels = every non-blank channel that isn't BF."""
        return [dict(c) for c in self.channels if c["index"] != self.bf_index]

    BF_ALIASES = ("bf", "brightfield", "bright field", "dic", "transmitted",
                 "trans", "phase")

    def _infer_bf_name(self, ch_names):
        """Return the channel name that looks like brightfield, or None if
        it can't be told apart (no match, or more than one match)."""
        matches = [n for n in ch_names if n.strip().lower() in self.BF_ALIASES]
        if len(matches) == 1:
            return matches[0]
        return None

    def _choose_dir(self, title):
        chooser = JFileChooser()
        chooser.setDialogTitle(title)
        chooser.setFileSelectionMode(JFileChooser.DIRECTORIES_ONLY)
        if chooser.showOpenDialog(None) == JFileChooser.APPROVE_OPTION:
            return chooser.getSelectedFile().getAbsolutePath()
        return None

    def channel_name(self, index_1based):
        for c in self.channels:
            if c["index"] == index_1based:
                return c["name"]
        return "Ch%d" % index_1based


# ===========================================================================
#  Session state manager
# ===========================================================================

class ZebrafishSession(object):
    """
    Central, OO state manager. Holds no module-level globals: the panel,
    listeners, and workflow all read/mutate this single object.
    """

    def __init__(self, config):
        self.config = config

        # Output layout: everything lives under output/<session name>/.
        self.session_name = config.session_name
        self.output_dir = os.path.join(config.output_dir, self.session_name)
        self.csv_path = os.path.join(self.output_dir,
                                     self.session_name + "_dataset.csv")
        self.roi_dir = os.path.join(self.output_dir, "ROIs")
        self.jpg_dir = os.path.join(self.output_dir, "Presentation_JPGs")
        for d in (self.output_dir, self.roi_dir, self.jpg_dir):
            ensure_dir(d)

        # No image queue: the tool always acts on whichever image is
        # currently active/focused in Fiji at the moment of each action.
        self.current_imp = None
        self.current_job = None       # {"label": title, "id": image_id}
        self.prepared_ids = set()     # images whose display we've auto-stretched

        # Mode + fish + channels.
        self.mode = config.mode
        # Fish are numbered/counted for the WHOLE session, not per image.
        self.fish_target = config.fish_total
        self.fish_committed = 0        # how many fish have been saved so far
        self.fish_id = 1               # ordinal of the fish currently in progress
        self.finished = False          # True once the user explicitly ends the session
        # Active fluorescent channels: list of dicts {index, name}.
        self.active_fl_channels = config.fl_channels()
        # Highest stack position we will ever need to reach.
        self.max_channel_index = max(c["index"] for c in config.channels)

        # ROIs collected for the *current* fish. Keyed by label.
        self.rois = {}                # e.g. {"Eye": roi, "GFP": roi, "BG_GFP": roi}

        # Wizard cursor.
        self.wizard_step = STEP_EYE
        self.wizard_fl_cursor = 0     # which active_fl_channel we are on
        self.wizard_phase = STEP_FL   # STEP_FL then STEP_BG within a channel

        # History: list of committed row dicts + context to allow rollback.
        # each: {image_id, label, fish_id, row, roi_zip, roi_names, rois_snapshot}
        self.history = []

        # RoiManager (singleton).
        self.rm = RoiManager.getInstance()
        if self.rm is None:
            self.rm = RoiManager()
        self.rm.reset()

        self._ensure_csv_header()

    # ---- CSV -------------------------------------------------------------

    def csv_header(self):
        # Every raw stat we compute (STAT_KEYS), not just the derived ones,
        # so further analysis can be done later without re-opening images.
        header = ["FileName", "FishID"]
        header += ["Eye%s" % k for k in STAT_KEYS]
        for ch in self.active_fl_channels:
            n = ch["name"]
            header += ["%s_%s" % (n, k) for k in STAT_KEYS]           # signal
            header += ["%s_BG%s" % (n, k) for k in STAT_KEYS]         # background
            header += ["%s_Corrected" % n]                            # derived
        return header

    def _ensure_csv_header(self):
        """Write header if the file is new; otherwise leave it (append mode)."""
        if not os.path.exists(self.csv_path):
            with open(self.csv_path, "wb") as f:
                w = csv.writer(f)
                w.writerow(self.csv_header())

    def append_row(self, row_dict):
        header = self.csv_header()
        with open(self.csv_path, "ab") as f:
            w = csv.writer(f)
            w.writerow([row_dict.get(h, "") for h in header])

    def rewrite_all_rows(self, rows):
        """Rewrite the CSV from scratch (used on rollback)."""
        header = self.csv_header()
        with open(self.csv_path, "wb") as f:
            w = csv.writer(f)
            w.writerow(header)
            for r in rows:
                w.writerow([r.get(h, "") for h in header])

    # ---- Image focus: always whatever the user currently has active ------

    def all_open_imps(self):
        """Every open image (for the [ / ] hop shortcut)."""
        ids = WindowManager.getIDList()
        if ids is None:
            return []
        return [WindowManager.getImage(i) for i in ids
                if WindowManager.getImage(i) is not None]

    def is_hyperstack(self, imp):
        return imp is not None and imp.getNChannels() > 1

    def sync_active_image(self):
        """Point the session at whatever image is CURRENTLY frontmost/active
        in Fiji. Call this right before any action (arming a channel,
        starting a fish) so "whatever image you're on" is always honored."""
        imp = WindowManager.getCurrentImage()
        if imp is None:
            return None
        if self.current_imp is not None and imp.getID() == self.current_imp.getID():
            return imp
        return self._focus(imp)

    def focus_image(self, imp):
        """Explicitly switch the drawing target to `imp` (hop / Go Back)."""
        if imp is None:
            return None
        return self._focus(imp)

    def _focus(self, imp):
        self.current_imp = imp
        self.current_job = {"label": imp.getTitle(), "id": imp.getID()}
        win = imp.getWindow()
        if win is not None:
            WindowManager.setCurrentWindow(win)
            win.toFront()
        # Single (non-hyper) stacks default to Manual, so the user can hop
        # between windows and assign each one to a channel. Hyperstacks use
        # the mode chosen at startup.
        self.mode = self.config.mode if self.is_hyperstack(imp) else MODE_MANUAL
        self._warn_if_missing_channels(imp)
        self._prepare_display(imp)
        log("Now on: %s (%s)" % (self.current_job["label"], self.mode))
        return imp

    def _warn_if_missing_channels(self, imp):
        """Warn if a HYPERSTACK has fewer channels than the setup requires.

        Plain stacks (channel dimension == 1) are deliberately excluded: their
        slices are treated as channels and we don't second-guess their layout.
        """
        nc = imp.getNChannels()
        if nc > 1 and nc < self.max_channel_index:
            msg = ("Image '%s' is a hyperstack with only %d channel(s), but your "
                   "setup uses channel %d.\n\nChannels above %d will fall back to "
                   "the last available one. Check this image's channel order."
                   % (imp.getTitle(), nc, self.max_channel_index, nc))
            log("WARNING: " + msg.replace("\n", " "))
            IJ.showMessage("Zebrafish Quant - channel check", msg)

    def _prepare_display(self, imp):
        """Make faint images visible: auto-stretch the display range (once per
        image) and open the Brightness/Contrast window for fine-tuning."""
        try:
            if imp.getID() not in self.prepared_ids:
                n = imp.getNChannels()
                if n > 1:
                    for c in range(1, n + 1):
                        imp.setC(c)
                        IJ.run(imp, "Enhance Contrast", "saturated=0.35")
                    imp.setC(1)
                else:
                    IJ.run(imp, "Enhance Contrast", "saturated=0.35")
                imp.updateAndDraw()
                self.prepared_ids.add(imp.getID())
            # Bring up the (non-modal) B&C dialog.
            IJ.run("Brightness/Contrast...")
        except Exception:
            log("Display prep failed: " + traceback.format_exc())

    # ---- Fish / ROI state ------------------------------------------------

    def reset_fish_rois(self):
        self.rois = {}
        # NOTE: deliberately does NOT clear the RoiManager window. export_rois()
        # already does its own rm.reset() + repopulate at each commit, so the
        # panel keeps showing the just-saved ROIs as visual confirmation until
        # the next commit overwrites it, instead of blanking immediately.

    def store_roi(self, key, roi):
        """Save an ROI together with the exact image + stack position it was
        drawn on (so it is later measured/exported at that exact spot, not
        wherever the display happens to be at commit time).

        This is what makes the single-stack / hop-between-windows workflow
        correct: each channel's ROI remembers its own source image, so it is
        later measured on THAT image rather than the currently focused one.
        """
        imp = self.current_imp
        if self.is_hyperstack(imp):
            channel, z, t = imp.getC(), imp.getZ(), imp.getT()
        else:
            channel, z, t = imp.getCurrentSlice(), None, None
        self.rois[key] = {"roi": roi.clone(), "imp": imp, "id": imp.getID(),
                          "channel": channel, "z": z, "t": t}

    def roi_of(self, key):
        """Return the raw Roi for a key, or None."""
        rec = self.rois.get(key)
        return rec["roi"] if rec else None

    def channel_done(self, name):
        """Whether a channel's main ROI (Eye, or the FL signal) is captured."""
        return name in self.rois

    def channels_progress(self):
        """(done, total) across BF + all active FL channels for this fish."""
        total = 1 + len(self.active_fl_channels)
        done = 1 if self.channel_done("Eye") else 0
        done += sum(1 for c in self.active_fl_channels
                    if self.channel_done(c["name"]))
        return done, total

    def all_channels_done(self):
        done, total = self.channels_progress()
        return total > 0 and done >= total

    def register_commit(self):
        """Record that the in-progress fish was saved; advance the counter."""
        self.fish_committed += 1
        self.fish_id = self.fish_committed + 1

    def session_complete(self):
        return self.fish_committed >= self.fish_target

    def next_fish(self):
        """Reset per-fish state so a new fish can be drawn (image unchanged)."""
        self._advance_hyperstack_frame()
        self.reset_fish_rois()
        self.reset_wizard()

    def _advance_hyperstack_frame(self):
        """For a true hyperstack with a T (frame) dimension, each fish is
        conventionally stored as its own frame (C = channel, T = fish). Move
        to the NEXT frame automatically when starting a new fish -- without
        this, every fish would keep measuring frame 1's data over and over.

        Plain stacks are untouched (the user navigates those manually; see
        arm_to_channel()'s docstring for why)."""
        imp = self.current_imp
        if imp is None or not self.is_hyperstack(imp):
            return
        nt = imp.getNFrames()
        if nt <= 1:
            return
        t = imp.getT()
        if t < nt:
            imp.setT(t + 1)
            imp.updateAndDraw()

    def add_custom_channel(self, name, index_1based):
        self.active_fl_channels.append({"index": index_1based, "name": name})
        # Header changes; migrate the CSV so existing rows keep aligning.
        self._migrate_csv_for_new_header()

    def _migrate_csv_for_new_header(self):
        """Re-read existing data and rewrite with the expanded header."""
        rows = []
        if os.path.exists(self.csv_path):
            with open(self.csv_path, "rb") as f:
                r = csv.DictReader(f)
                for line in r:
                    rows.append(dict(line))
        self.rewrite_all_rows(rows)

    # ---- Wizard cursor ---------------------------------------------------

    def reset_wizard(self):
        self.wizard_step = STEP_EYE
        self.wizard_fl_cursor = 0
        self.wizard_phase = STEP_FL

    def current_wizard_channel(self):
        if 0 <= self.wizard_fl_cursor < len(self.active_fl_channels):
            return self.active_fl_channels[self.wizard_fl_cursor]
        return None

    # ---- Mode ------------------------------------------------------------

    def toggle_mode(self):
        self.mode = MODE_MANUAL if self.mode == MODE_WIZARD else MODE_WIZARD
        return self.mode


# ===========================================================================
#  Quantification engine
# ===========================================================================

class QuantEngine(object):
    """Turns the collected ROIs into a CSV row and computes corrected intensity."""

    def __init__(self, session):
        self.s = session

    def _measure(self, rec):
        """Measure a stored ROI record on its OWN image + exact position."""
        imp = rec["imp"]
        set_channel(imp, rec["channel"], rec.get("z"), rec.get("t"))
        return measure_roi(imp, rec["roi"])

    def _full_stats(self, rec):
        """Every raw stat (STAT_KEYS) for one stored ROI record."""
        stats = self._measure(rec)
        roi, imp = rec["roi"], rec["imp"]
        try:
            roi.setImage(imp)   # so getLength() uses the image's calibration
            perimeter = roi.getLength()
        except Exception:
            perimeter = 0.0
        return {
            "Area": stats.area, "Mean": stats.mean, "StdDev": stats.stdDev,
            "Median": stats.median, "Mode": stats.dmode, "Min": stats.min,
            "Max": stats.max, "Perimeter": perimeter,
            "IntDen": stats.area * stats.mean,          # calibrated
            "RawIntDen": stats.pixelCount * stats.mean, # uncalibrated pixel count
            "Skewness": stats.skewness, "Kurtosis": stats.kurtosis,
        }

    def build_row(self):
        s = self.s
        row = {"FileName": s.current_job["label"],
               "FishID": "Fish %d" % s.fish_id}

        # Eye: every raw stat, not just area, for later analysis.
        eye_rec = s.rois.get("Eye")
        if eye_rec is not None:
            vals = self._full_stats(eye_rec)
            for k in STAT_KEYS:
                row["Eye%s" % k] = round(vals[k], 4)
        else:
            for k in STAT_KEYS:
                row["Eye%s" % k] = ""

        # Each fluorescent channel: full signal stats + full BG stats +
        # the derived corrected intensity.
        for ch in s.active_fl_channels:
            name = ch["name"]
            fl_rec = s.rois.get(name)
            bg_rec = s.rois.get("BG_" + name)
            if fl_rec is None:
                continue

            fl_vals = self._full_stats(fl_rec)
            for k in STAT_KEYS:
                row["%s_%s" % (name, k)] = round(fl_vals[k], 4)

            mean_bg = 0.0
            if bg_rec is not None:
                bg_vals = self._full_stats(bg_rec)
                mean_bg = bg_vals["Mean"]
                for k in STAT_KEYS:
                    row["%s_BG%s" % (name, k)] = round(bg_vals[k], 4)
            else:
                for k in STAT_KEYS:
                    row["%s_BG%s" % (name, k)] = ""

            corrected = fl_vals["IntDen"] - (mean_bg * fl_vals["Area"])
            row["%s_Corrected" % name] = round(corrected, 4)

        return row


# ===========================================================================
#  Export
# ===========================================================================

class Exporter(object):
    """Writes ROI zips and targeted presentation JPEGs."""

    def __init__(self, session):
        self.s = session

    def _safe_stub(self):
        label = self.s.current_job["label"]
        stub = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        return "%s_%s_Fish%d" % (self.s.session_name, stub, self.s.fish_id)

    def master_zip_path(self):
        """The single, session-wide ROI archive (all fish, all channels)."""
        return os.path.join(self.s.roi_dir, self.s.session_name + "_all_ROIs.zip")

    def export_rois(self):
        """Add this fish's ROIs into the ONE session-wide RoiManager/zip
        (never a separate file per fish), each uniquely named so nothing
        collides across fish or images. Returns (zip_path, qualified_names)
        so the caller can track/undo exactly what this commit added."""
        s = self.s
        rm = s.rm
        stub = self._safe_stub()

        ordered = []
        if "Eye" in s.rois:
            ordered.append("Eye")
        for ch in s.active_fl_channels:
            n = ch["name"]
            if n in s.rois:
                ordered.append(n)
            if ("BG_" + n) in s.rois:
                ordered.append("BG_" + n)

        qualified_names = []
        for name in ordered:
            rec = s.rois[name]
            roi = rec["roi"].clone()
            qualified = "%s__%s" % (stub, name)
            roi.setName(qualified)
            # Preserve the EXACT position this ROI was drawn on -- for a plain
            # stack (no fixed per-channel slice, e.g. one big stack holding
            # every fish and channel together) AND for a hyperstack (full
            # c/z/t), so re-measuring or a later Go Back lands on the right
            # plane instead of guessing.
            if s.is_hyperstack(rec["imp"]):
                roi.setPosition(rec["channel"], rec.get("z") or 1, rec.get("t") or 1)
            else:
                roi.setPosition(rec["channel"])
            rm.addRoi(roi)
            qualified_names.append(qualified)

        zip_path = self.master_zip_path()
        if rm.getCount() > 0:
            rm.deselect()
            rm.runCommand("Save", zip_path)
        return zip_path, qualified_names

    def export_jpgs(self):
        s = self.s
        stub = self._safe_stub()

        # 1) Brightfield with the eye overlay only (on the eye's own image).
        eye_rec = s.rois.get("Eye")
        if eye_rec is not None:
            self._save_overlay_jpg(eye_rec["imp"], eye_rec["channel"],
                                   [(eye_rec["roi"], Color.YELLOW)],
                                   os.path.join(s.jpg_dir, stub + "_BF_Eye.jpg"))

        # 2) Each FL channel with its FL + coupled BG ROI together, on the
        #    image that channel was analyzed on.
        for ch in s.active_fl_channels:
            n = ch["name"]
            fl_rec = s.rois.get(n)
            bg_rec = s.rois.get("BG_" + n)
            if fl_rec is None:
                continue
            pairs = [(fl_rec["roi"], Color.GREEN)]
            if bg_rec is not None:
                pairs.append((bg_rec["roi"], Color.CYAN))
            self._save_overlay_jpg(fl_rec["imp"], fl_rec["channel"], pairs,
                                   os.path.join(s.jpg_dir, "%s_%s.jpg" % (stub, n)))

    def _save_overlay_jpg(self, imp, channel_1based, roi_color_pairs, out_path):
        set_channel(imp, channel_1based)
        # Flatten a single-channel snapshot with the given ROI overlay.
        dup = imp.duplicate()
        try:
            set_channel(dup, channel_1based)
            ov = Overlay()
            for roi, color in roi_color_pairs:
                r = roi.clone()
                r.setStrokeColor(color)
                r.setStrokeWidth(2)
                ov.add(r)
            dup.setOverlay(ov)
            flat = dup.flatten()
            FileSaver.setJpegQuality(JPEG_QUALITY)   # static setting, no dialog
            FileSaver(flat).saveAsJpeg(out_path)
            flat.close()
        finally:
            dup.close()


# ===========================================================================
#  Listeners (wire user gestures into the state machine)
# ===========================================================================

class WandRoiListener(RoiListener):
    """
    Detects when the user completes a Magic Wand selection so the wizard can
    auto-couple the background rectangle. RoiListener fires on ROI changes.
    """

    def __init__(self, controller):
        self.controller = controller

    def roiModified(self, imp, id_):
        # id_ == RoiListener.COMPLETED fires when a selection finishes.
        try:
            if id_ == RoiListener.COMPLETED:
                self.controller.on_roi_completed(imp)
        except Exception:
            log(traceback.format_exc())


class HotkeyListener(KeyAdapter):
    """Global-ish key listener attached to the image canvas and the panel."""

    def __init__(self, controller):
        self.controller = controller

    def keyPressed(self, event):
        try:
            self.controller.on_key(event)
        except Exception:
            log(traceback.format_exc())


# ===========================================================================
#  Workflow controller
# ===========================================================================

class WorkflowController(object):
    """
    Orchestrates arming tools/channels and reacting to user gestures for both
    modes. The panel and listeners call into this object.
    """

    def __init__(self, session):
        self.s = session
        self.engine = QuantEngine(session)
        self.exporter = Exporter(session)
        self.panel = None            # set by main()
        self.roi_listener = WandRoiListener(self)   # kept for reference; not armed
        self.key_listener = HotkeyListener(self)
        # Nothing auto-captures anymore: every ROI waits for Space to be accepted.
        # Manual-mode pending-capture tracking.
        self._manual_channel_name = None
        self._manual_phase = None       # "eye" | "signal" | "bg" | None
        # "Slice lock": the exact stack position we armed on. When there is
        # only one image (a plain stack holding every fish/channel), nothing
        # stops the user from scrolling the stack mid-draw and accidentally
        # capturing the WRONG plane. We remember the armed slice and refuse
        # to accept if it drifts before SPACE is pressed.
        self._armed_image_id = None
        self._armed_slice = None

    # ---- Lifecycle -------------------------------------------------------

    def start(self):
        if self.s.sync_active_image() is None:
            IJ.error("No active image.\nClick an image window in Fiji, then re-run.")
            return
        self._attach_key_listener()
        self.begin_wizard_or_manual()

    def _attach_key_listener(self):
        imp = self.s.current_imp
        if imp is None:
            return
        win = imp.getWindow()
        if win is not None:
            canvas = win.getCanvas()
            # Avoid stacking duplicate listeners across images.
            for kl in list(canvas.getKeyListeners()):
                if isinstance(kl, HotkeyListener):
                    canvas.removeKeyListener(kl)
            canvas.addKeyListener(self.key_listener)

    def begin_wizard_or_manual(self):
        """Start (or continue) the current fish on whatever image is
        currently active in Fiji right now."""
        self.s.sync_active_image()
        if self.s.mode == MODE_WIZARD:
            self.s.reset_wizard()
            self.advance_wizard()
        else:
            self.enter_manual()
        self._refresh_panel()

    # ---- Wizard ----------------------------------------------------------

    def advance_wizard(self):
        """Arm the tool/channel for the current wizard step + phase.

        Arming never captures anything. The user draws/adjusts freely and
        presses Space (next_step) to accept the ROI and move on.
        """
        s = self.s
        if s.mode != MODE_WIZARD:
            return
        step = s.wizard_step

        if step == STEP_EYE:
            arm_to_channel(s.current_imp, s.config.bf_index)
            self._clear_threshold(s.current_imp)   # no red overlay on brightfield
            arm_tool(TOOL_OVAL)
            self._status("Wizard: draw/adjust the EYE ellipse on %s, then press SPACE."
                         % s.config.channel_name(s.config.bf_index))

        elif step == STEP_FL:
            ch = s.current_wizard_channel()
            if ch is None:
                s.wizard_step = STEP_DONE
                self._status("Wizard: all channels done. Press ENTER to commit fish.")
                return
            if s.wizard_phase == STEP_BG:
                # Background phase: clear the threshold overlay (it was only
                # relevant for wand-clicking the signal) and arm the rectangle.
                self._clear_threshold(s.current_imp)
                arm_tool(TOOL_RECT)
                self._status("Wizard: draw/adjust the BACKGROUND rectangle for %s, "
                             "then press SPACE." % ch["name"])
            else:
                # Signal phase: threshold + wand, adjust as needed.
                arm_to_channel(s.current_imp, ch["index"])
                self._apply_threshold(s.current_imp)
                arm_tool(TOOL_WAND)
                self._status("Wizard: adjust threshold / WAND-click %s. Re-click as "
                             "needed, then press SPACE to accept." % ch["name"])

        elif step == STEP_DONE:
            self._status("Wizard: press ENTER (or Commit button) to save this fish.")

        # Show the already-captured ROI for this step (if we're revisiting it),
        # otherwise clear any stale selection so it can't be captured by accident.
        self._restore_step_roi()

    def _current_step_roi_key(self):
        """The s.rois key that corresponds to the current wizard step, if any."""
        s = self.s
        if s.mode != MODE_WIZARD:
            return None
        if s.wizard_step == STEP_EYE:
            return "Eye"
        if s.wizard_step == STEP_FL:
            ch = s.current_wizard_channel()
            if ch is None:
                return None
            return ("BG_" + ch["name"]) if s.wizard_phase == STEP_BG else ch["name"]
        return None

    def _restore_step_roi(self):
        s = self.s
        imp = s.current_imp
        if imp is None:
            return
        key = self._current_step_roi_key()
        if key and key in s.rois:
            imp.setRoi(s.rois[key]["roi"])
        else:
            imp.deleteRoi()

    def prev_step(self):
        """Go BACK one wizard step/channel within the current fish.

        Captured ROIs are kept, so stepping back lets you review or redo a
        previous channel without losing anything.
        """
        s = self.s
        if s.mode != MODE_WIZARD:
            self._status("Back-a-step works in Wizard mode; in Manual just press "
                         "the channel key again.")
            return

        # Save whatever is currently drawn before moving, so edits aren't lost.
        self._capture_pending()

        n = len(s.active_fl_channels)
        if s.wizard_step == STEP_EYE:
            self._status("Already at the first step (eye).")
            return

        if s.wizard_step == STEP_DONE:
            if n == 0:
                s.wizard_step = STEP_EYE
            else:
                s.wizard_step = STEP_FL
                s.wizard_fl_cursor = n - 1
                s.wizard_phase = STEP_BG
            self.advance_wizard()
            self._refresh_panel()
            return

        # step == STEP_FL
        if s.wizard_phase == STEP_BG:
            s.wizard_phase = STEP_FL          # back to this channel's signal
        else:
            if s.wizard_fl_cursor == 0:
                s.wizard_step = STEP_EYE      # back to the eye
            else:
                s.wizard_fl_cursor -= 1       # back to previous channel's BG
                s.wizard_phase = STEP_BG
        self.advance_wizard()
        self._refresh_panel()

    def _apply_threshold(self, imp):
        """Seed a threshold AND open the interactive Threshold window so the
        user can drag the sliders before wand-clicking.

        Rather than ImageJ's data-driven "Default"/Otsu-style auto-threshold
        (which can pick a degenerate split -- e.g. minThreshold==maxThreshold
        -- and show nothing highlighted on some images), we set a direct,
        predictable cutoff: the brightest THRESHOLD_PERCENT of this slice's
        own intensity range (default 95-100%), so only strong signal is ever
        selected, and it's never simply invisible.

        Snapshots and restores the display (Brightness/Contrast) range so
        thresholding never alters it, even as an incidental side effect.
        """
        try:
            ip = imp.getProcessor()
            disp_min, disp_max = ip.getMin(), ip.getMax()

            # Open (or refocus) the Threshold window FIRST. If it's already
            # open from an earlier arm, it listens for image/channel changes
            # and auto-recomputes its OWN threshold the moment the position
            # changes (which arm_to_channel() just did) -- so setting our
            # value before this would get silently overwritten. Setting it
            # AFTER guarantees our percentage-based cutoff is what sticks.
            IJ.run(imp, "Threshold...", "")
            ip = imp.getProcessor()   # re-fetch: position may have "settled" now
            stats = ip.getStatistics()
            lo = stats.min + THRESHOLD_PERCENT * (stats.max - stats.min)
            hi = stats.max
            ip.setThreshold(lo, hi, ip.RED_LUT)
            ip.setMinAndMax(disp_min, disp_max)   # B&C untouched
            imp.updateAndDraw()
        except Exception:
            log("Auto-threshold failed: " + traceback.format_exc())

    def _clear_threshold(self, imp):
        """Remove any threshold overlay (e.g. when going back to the eye/BF,
        or moving on to draw a background box), WITHOUT touching the
        Brightness/Contrast display range."""
        try:
            ip = imp.getProcessor()
            disp_min, disp_max = ip.getMin(), ip.getMax()
            if ip is not None:
                ip.resetThreshold()
                ip.setMinAndMax(disp_min, disp_max)   # B&C untouched
            imp.updateAndDraw()
        except Exception:
            log("Clear-threshold failed: " + traceback.format_exc())

    def on_roi_completed(self, imp):
        """No-op. ROIs are never captured automatically; Space accepts them."""
        pass

    # ---- Stepwise acceptance (Space) -------------------------------------

    def next_step(self):
        """
        Accept the currently drawn ROI and advance one step. This is the SPACE
        action: nothing captures until the user is happy with the selection.

        A direct channel action (a button/number-key press) always takes
        priority: if one is pending, SPACE accepts it regardless of mode.
        Otherwise, in Wizard mode, SPACE advances the guided sequence.
        """
        s = self.s
        if s.current_imp is None:
            return

        if self._manual_phase is not None:
            self._manual_next_step()
        elif s.mode == MODE_WIZARD:
            self._wizard_next_step()
        elif s.all_channels_done():
            # Every configured channel has been captured: SPACE with nothing
            # pending just commits and moves on to the next fish.
            self.on_commit_pressed()
        else:
            done, total = s.channels_progress()
            self._status("Pick a channel (button or number key) first. (%d/%d done)"
                         % (done, total))
        self._refresh_panel()

    def _wizard_next_step(self):
        s = self.s
        step = s.wizard_step

        if step == STEP_EYE:
            roi = s.current_imp.getRoi()
            if roi is None:
                self._status("Draw the eye ellipse first, then press SPACE.")
                return
            s.store_roi("Eye", roi)
            s.current_imp.deleteRoi()  # channel done: clear from view, kept in s.rois
            s.wizard_step = STEP_FL
            s.wizard_fl_cursor = 0
            s.wizard_phase = STEP_FL
            self.advance_wizard()
            return

        if step == STEP_FL:
            ch = s.current_wizard_channel()
            if ch is None:
                s.wizard_step = STEP_DONE
                self.advance_wizard()
                return
            roi = s.current_imp.getRoi()
            if s.wizard_phase == STEP_BG:
                if roi is None:
                    self._status("Draw the background rectangle first, then SPACE.")
                    return
                s.store_roi("BG_" + ch["name"], roi)
                s.current_imp.deleteRoi()  # channel done: clear from view, kept in s.rois
                s.wizard_fl_cursor += 1
                if s.wizard_fl_cursor >= len(s.active_fl_channels):
                    s.wizard_step = STEP_DONE
                else:
                    s.wizard_phase = STEP_FL
                self.advance_wizard()
            else:
                if roi is None:
                    self._status("WAND-click the %s signal first, then SPACE."
                                 % ch["name"])
                    return
                s.store_roi(ch["name"], roi)
                s.wizard_phase = STEP_BG
                self.advance_wizard()
            return

        if step == STEP_DONE:
            # Convenience: SPACE at the end also commits.
            self.commit_fish()

    def _manual_next_step(self):
        """SPACE accepts whatever the last direct channel action armed."""
        s = self.s
        phase = self._manual_phase
        imp = s.current_imp
        roi = imp.getRoi()

        # Slice-lock: if the stack scrolled away from where we armed, refuse
        # to accept -- silently measuring the wrong plane is worse than
        # making the user scroll back (or re-arm) and try again.
        if phase is not None and self._slice_drifted(imp):
            self._status("STOPPED: the stack moved to slice %d (armed on slice "
                         "%d). Scroll back to slice %d and press SPACE again, "
                         "or press the channel key to re-arm here."
                         % (imp.getCurrentSlice(), self._armed_slice,
                           self._armed_slice))
            return

        if phase == "eye":
            if roi is None:
                self._status("Draw the eye ellipse first, then SPACE.")
                return
            s.store_roi("Eye", roi)
            imp.deleteRoi()  # channel done: clear from view, kept in s.rois
            self._manual_phase = None
            self._armed_image_id = None
            self._armed_slice = None
            self._status_after_accept()

        elif phase == "signal":
            if roi is None:
                self._status("WAND-click the signal first, then SPACE.")
                return
            s.store_roi(self._manual_channel_name, roi)
            self._clear_threshold(imp)   # no red overlay for the BG box
            arm_tool(TOOL_RECT)
            self._manual_phase = "bg"
            self._lock_slice(imp)   # re-lock: BG must be on the same plane
            self._status("LOCKED on slice %d. Signal saved. Draw the BG "
                         "rectangle, then SPACE." % imp.getCurrentSlice())

        elif phase == "bg":
            if roi is None:
                self._status("Draw the background rectangle first, then SPACE.")
                return
            s.store_roi("BG_" + self._manual_channel_name, roi)
            imp.deleteRoi()  # channel done: clear from view, kept in s.rois
            self._manual_phase = None
            self._armed_image_id = None
            self._armed_slice = None
            self._status_after_accept()
        else:
            done, total = s.channels_progress()
            self._status("Nothing to accept. Press a channel key first. (%d/%d done)"
                         % (done, total))

    def _status_after_accept(self):
        s = self.s
        done, total = s.channels_progress()
        if done >= total:
            self._status("All %d/%d channels done. Press SPACE or ENTER to save "
                         "this fish." % (done, total))
        else:
            self._status("%d/%d channels done. Pick the next channel, or SPACE "
                         "when finished." % (done, total))

    # ---- Skipping (S) ----------------------------------------------------

    def skip_current(self):
        """Skip the current step without recording an ROI for it.

        Wizard: skips the eye, a whole FL channel, or just a background,
        depending on where you are. Manual: abandons whatever is armed.
        """
        s = self.s
        if s.current_imp is None:
            return
        if s.mode == MODE_WIZARD:
            self._wizard_skip()
        else:
            self._manual_skip()
        # Drop any leftover selection so it can't be captured by the next step.
        if s.current_imp is not None:
            s.current_imp.deleteRoi()
        self._refresh_panel()

    def _wizard_skip(self):
        s = self.s
        step = s.wizard_step

        if step == STEP_EYE:
            s.rois.pop("Eye", None)
            s.wizard_step = STEP_FL
            s.wizard_fl_cursor = 0
            s.wizard_phase = STEP_FL
            self.advance_wizard()
            self._status("Skipped the eye. On to fluorescence.")
            return

        if step == STEP_FL:
            ch = s.current_wizard_channel()
            if ch is None:
                s.wizard_step = STEP_DONE
                self.advance_wizard()
                return
            if s.wizard_phase == STEP_BG:
                # Keep the signal we already accepted; just skip its background.
                msg = "Skipped background for %s (kept its signal)." % ch["name"]
            else:
                # Skip the whole channel: drop any partial ROIs for it.
                s.rois.pop(ch["name"], None)
                s.rois.pop("BG_" + ch["name"], None)
                msg = "Skipped channel %s entirely." % ch["name"]
            s.wizard_fl_cursor += 1
            if s.wizard_fl_cursor >= len(s.active_fl_channels):
                s.wizard_step = STEP_DONE
            else:
                s.wizard_phase = STEP_FL
            self.advance_wizard()
            self._status(msg)
            return

        # STEP_DONE: nothing to skip.
        self._status("Nothing to skip. Press ENTER to commit.")

    def _manual_skip(self):
        s = self.s
        phase = self._manual_phase
        name = self._manual_channel_name
        if phase == "eye":
            s.rois.pop("Eye", None)
        elif phase == "signal" and name:
            # Signal not yet accepted -> drop the whole channel.
            s.rois.pop(name, None)
            s.rois.pop("BG_" + name, None)
        # phase == "bg": signal is already saved; skipping just drops the BG.
        self._manual_phase = None
        self._manual_channel_name = None
        self._armed_image_id = None
        self._armed_slice = None
        self._status("Manual: skipped. Pick a channel key, or ENTER to commit.")

    def _capture_pending(self):
        """Store the current selection into its slot without advancing.

        Used when the user hits ENTER/Commit mid-step so the on-screen ROI
        isn't silently lost.
        """
        s = self.s
        roi = s.current_imp.getRoi() if s.current_imp else None
        if roi is None:
            return
        if s.mode == MODE_WIZARD:
            step = s.wizard_step
            if step == STEP_EYE:
                s.store_roi("Eye", roi)
            elif step == STEP_FL:
                ch = s.current_wizard_channel()
                if ch is not None:
                    key = ("BG_" + ch["name"]) if s.wizard_phase == STEP_BG else ch["name"]
                    s.store_roi(key, roi)
        else:
            if self._manual_phase == "eye":
                s.store_roi("Eye", roi)
            elif self._manual_phase == "signal":
                s.store_roi(self._manual_channel_name, roi)
            elif self._manual_phase == "bg":
                s.store_roi("BG_" + self._manual_channel_name, roi)

    # ---- Manual mode -----------------------------------------------------

    def enter_manual(self):
        self.s.mode = MODE_MANUAL
        self._manual_channel_name = None
        self._manual_phase = None
        self._armed_image_id = None
        self._armed_slice = None
        self._status("MANUAL: [1]=Eye  [2..]=FL channels  SPACE=accept  S=skip  "
                     "ENTER=commit fish.")

    def _lock_slice(self, imp):
        """Remember the exact slice we armed on, so accepting later can
        detect (and refuse) an accidental scroll away from it."""
        self._armed_image_id = imp.getID()
        self._armed_slice = imp.getCurrentSlice()

    def _slice_drifted(self, imp):
        """True if the stack has moved since arming (single-image workflow
        where nothing else stops the user from scrolling mid-draw)."""
        if self._armed_image_id is None or imp is None:
            return False
        if imp.getID() != self._armed_image_id:
            return False   # a different image entirely; not a "drift"
        return imp.getCurrentSlice() != self._armed_slice

    def arm_eye(self):
        """Direct channel action: arm the eye ellipse on whatever image is
        CURRENTLY ACTIVE in Fiji right now (button or number-key '1')."""
        s = self.s
        imp = s.sync_active_image()
        if imp is None:
            self._status("Click an image window in Fiji first.")
            return
        arm_to_channel(imp, s.config.bf_index)
        self._clear_threshold(imp)   # no red overlay on brightfield
        arm_tool(TOOL_OVAL)
        self._manual_channel_name = None
        self._manual_phase = "eye"
        self._lock_slice(imp)
        self._status("LOCKED on slice %d of '%s'. Draw the EYE ellipse, then "
                     "SPACE. Do not scroll the stack until then."
                     % (imp.getCurrentSlice(), imp.getTitle()))
        self._refresh_panel()

    def arm_fl(self, ch):
        """Direct channel action: arm wand+threshold for channel `ch` on
        whatever image is CURRENTLY ACTIVE in Fiji right now."""
        s = self.s
        imp = s.sync_active_image()
        if imp is None:
            self._status("Click an image window in Fiji first.")
            return
        arm_to_channel(imp, ch["index"])
        self._apply_threshold(imp)
        arm_tool(TOOL_WAND)
        self._manual_channel_name = ch["name"]
        self._manual_phase = "signal"
        self._lock_slice(imp)
        self._status("LOCKED on slice %d of '%s'. WAND-click %s, then SPACE. "
                     "Do not scroll the stack until then."
                     % (imp.getCurrentSlice(), imp.getTitle(), ch["name"]))
        self._refresh_panel()

    def _manual_eye(self):
        """Digit key '1'."""
        self.arm_eye()

    def _manual_fl(self, slot):
        """Digit key '2'.. -> slot is 1-based position among active FL channels."""
        s = self.s
        if slot < 1 or slot > len(s.active_fl_channels):
            self._status("No FL channel in slot %d." % slot)
            return
        self.arm_fl(s.active_fl_channels[slot - 1])

    # ---- Key routing -----------------------------------------------------

    def on_key(self, event):
        code = event.getKeyCode()

        # SPACE = accept current ROI and advance one step (never commits early).
        if code == KeyEvent.VK_SPACE:
            event.consume()
            self.next_step()
            return

        # S = skip the current step (channel / eye / background).
        if code == KeyEvent.VK_S:
            event.consume()
            self.skip_current()
            return

        # BACKSPACE = step back to the previous channel/step within this fish.
        if code == KeyEvent.VK_BACK_SPACE:
            event.consume()
            self.prev_step()
            return

        # [ and ] hop between open image windows (for single-stack workflows).
        if code == KeyEvent.VK_OPEN_BRACKET:
            event.consume()
            self.hop_image(-1)
            return
        if code == KeyEvent.VK_CLOSE_BRACKET:
            event.consume()
            self.hop_image(1)
            return

        # ENTER = commit the whole fish (captures the on-screen ROI first).
        if code == KeyEvent.VK_ENTER:
            event.consume()
            self.on_commit_pressed()
            return

        # Digit keys are DIRECT channel actions: they always arm on whatever
        # image is currently active in Fiji, regardless of mode (Wizard or
        # not) -- same as clicking the matching channel button.
        digit = None
        if KeyEvent.VK_1 <= code <= KeyEvent.VK_9:
            digit = code - KeyEvent.VK_0

        if digit is not None:
            event.consume()
            if digit == 1:
                self._manual_eye()
            else:
                self._manual_fl(digit - 1)

    # ---- Commit / next fish ----------------------------------------------

    def on_commit_pressed(self):
        # Grab whatever ROI is currently on screen so it isn't lost, then
        # finalize. ENTER can be pressed at any step to commit early.
        self._capture_pending()
        self.commit_fish()

    def commit_fish(self):
        """Compute, export, record history, then continue toward the total."""
        s = self.s
        if s.finished:
            self._status("Session already finished (target reached). Use "
                         "Skip/Add-fish prompts, or restart for a new session.")
            return

        fish_id_saved = s.fish_id
        try:
            row = self.engine.build_row()
            zip_path, roi_names = self.exporter.export_rois()
            self.exporter.export_jpgs()
            s.append_row(row)
            s.history.append({"image_id": s.current_job["id"],
                              "label": s.current_job["label"],
                              "fish_id": fish_id_saved,
                              "row": row,
                              "roi_zip": zip_path,
                              "roi_names": roi_names,
                              "rois_snapshot": dict(s.rois)})
            log("Committed %s / Fish %d" % (s.current_job["label"], fish_id_saved))
        except Exception:
            IJ.error("Commit failed:\n" + traceback.format_exc())
            return

        s.register_commit()   # advances the SESSION-WIDE fish counter

        if s.session_complete():
            self._finish_session()
            return

        # Never auto-navigate to a "next image": stay right where the user
        # is. begin_wizard_or_manual() re-syncs to whatever image is
        # currently active in Fiji, so if they've clicked elsewhere it's
        # honored; if not, they keep working on the same one.
        s.next_fish()
        self.begin_wizard_or_manual()
        self._status("Fish %d of %d saved. Click/hop to any image for the next one."
                     % (s.fish_committed, s.fish_target))

    def _finish_session(self):
        """Called the moment the fish target is reached. Forces an explicit
        choice (instead of silently letting further commits happen against
        stale/uncleared fish state, which was the "Add Another Fish after
        done breaks" bug): either bump the target and keep going cleanly, or
        stop for real."""
        s = self.s
        choice = JOptionPane.showConfirmDialog(
            None,
            "Target of %d fish reached (session total so far: %d).\n\n"
            "Add one more fish, or finish the session now?"
            % (s.fish_target, s.fish_committed),
            "Zebrafish Quant - Target Reached",
            JOptionPane.YES_NO_OPTION)

        if choice == JOptionPane.YES_OPTION:
            s.fish_target += 1
            s.next_fish()          # clean reset: no stale ROIs from the last fish
            self.begin_wizard_or_manual()
            self._status("Continuing: working on Fish %d of %d."
                         % (s.fish_id, s.fish_target))
        else:
            s.finished = True
            self._status("Session complete: %d/%d fish saved."
                         % (s.fish_committed, s.fish_target))
            IJ.showMessage("Zebrafish Quant",
                           "Session complete.\n%d fish saved.\nCSV: %s"
                           % (s.fish_committed, s.csv_path))

    def skip_fish(self):
        """Reduce the remaining target by one and discard whatever is
        in-progress for the CURRENT fish -- for when too many fish were
        configured at startup and there simply aren't that many to quantify."""
        s = self.s
        if s.finished:
            self._status("Session already finished.")
            return
        if s.fish_target <= s.fish_committed:
            self._status("Nothing to skip: target already matches the %d fish "
                         "already saved." % s.fish_committed)
            return

        s.fish_target -= 1
        s.reset_fish_rois()
        s.reset_wizard()
        self._manual_phase = None
        self._manual_channel_name = None

        if s.session_complete():
            self._finish_session()
        else:
            self.begin_wizard_or_manual()
            self._status("Skipped a fish slot. Target is now %d (saved so far: %d)."
                         % (s.fish_target, s.fish_committed))

    # ---- Image hopping (single-stack workflow) --------------------------

    def hop_image(self, delta):
        """Move the drawing target to another open window without resetting
        the current fish. Lets the user assign different single-channel images
        to different channels."""
        s = self.s
        imps = s.all_open_imps()
        if len(imps) < 2:
            self._status("Only one image open; nothing to hop to.")
            return
        cur_id = s.current_imp.getID() if s.current_imp else None
        idx = 0
        for i, imp in enumerate(imps):
            if imp.getID() == cur_id:
                idx = i
                break
        nxt = imps[(idx + delta) % len(imps)]
        s.focus_image(nxt)
        self._attach_key_listener()
        self._status("Now drawing on: %s  (assign a channel with a number key)."
                     % nxt.getTitle())
        self._refresh_panel()

    # ---- Panel button actions -------------------------------------------

    def add_another_fish(self):
        """Commit the current fish's data, then start a new fish (same image)."""
        s = self.s
        if s.finished:
            self._status("Session already finished (target reached).")
            return

        # Capture whatever ROI is on screen before saving this fish.
        self._capture_pending()

        fish_id_saved = s.fish_id
        try:
            row = self.engine.build_row()
            zip_path, roi_names = self.exporter.export_rois()
            self.exporter.export_jpgs()
            s.append_row(row)
            s.history.append({"image_id": s.current_job["id"],
                              "label": s.current_job["label"],
                              "fish_id": fish_id_saved,
                              "row": row,
                              "roi_zip": zip_path,
                              "roi_names": roi_names,
                              "rois_snapshot": dict(s.rois)})
        except Exception:
            IJ.error("Add-fish commit failed:\n" + traceback.format_exc())
            return

        s.register_commit()   # advances the SESSION-WIDE fish counter

        if s.session_complete():
            self._finish_session()
            return

        s.next_fish()          # resets ROIs & wizard; image stays the same
        self.begin_wizard_or_manual()
        self._status("Fish %d of %d saved." % (s.fish_committed, s.fish_target))

    def add_custom_channel(self):
        gd = GenericDialog("Add Custom Channel")
        gd.addStringField("Channel name:", "MyTarget", 12)
        gd.addNumericField("Image channel #:", self.s.current_imp.getNChannels(), 0)
        gd.showDialog()
        if gd.wasCanceled():
            return
        name = gd.getNextString().strip()
        idx = int(gd.getNextNumber())
        if name:
            self.s.add_custom_channel(name, idx)
            self._status("Added custom channel '%s' (ch %d)." % (name, idx))
            self._refresh_panel()

    def go_back(self):
        """Roll back one committed row: remove it from the CSV, remove its
        ROIs from the single session-wide zip, and restore its exact ROIs
        (from the in-memory snapshot -- no re-parsing any file needed) so
        the user can correct and re-commit."""
        s = self.s
        if not s.history:
            self._status("History is empty; nothing to undo.")
            return

        last = s.history.pop()

        # Rewrite CSV without the last committed row.
        rows = [h["row"] for h in s.history]
        s.rewrite_all_rows(rows)

        # Remove this fish's entries from the shared ROI manager / master zip.
        self._remove_from_master_zip(last.get("roi_names", []))

        # Re-select the previous image window (it must still be open).
        imp = WindowManager.getImage(last["image_id"])
        if s.focus_image(imp) is None:
            # Window was closed; restore history entry and bail gracefully.
            s.history.append(last)
            s.rewrite_all_rows([h["row"] for h in s.history])
            self._status("Cannot go back: the image '%s' was closed."
                         % last["label"])
            return
        self._attach_key_listener()
        s.fish_id = last["fish_id"]
        s.fish_committed = max(0, s.fish_committed - 1)   # undo the counted commit
        s.finished = False   # a rollback always reopens the session

        # Restore this fish's exact ROIs for further editing.
        snapshot = dict(last.get("rois_snapshot", {}))
        s.rois = snapshot

        # If fish-per-frame is in use (hyperstack, T = fish), jump back to
        # THIS fish's frame so redrawing lands on the right plane.
        if snapshot and s.is_hyperstack(imp):
            any_rec = next(iter(snapshot.values()))
            t = any_rec.get("t")
            if t:
                imp.setT(t)
                imp.updateAndDraw()

        # Re-arm at the eye step so the user can correct.
        s.reset_wizard()
        self.begin_wizard_or_manual()
        self._status("Rolled back to %s / Fish %d." %
                     (s.current_job["label"], s.fish_id))

    def _remove_from_master_zip(self, qualified_names):
        """Delete the given entries (by name) from the session-wide
        RoiManager and re-save the single zip, so an undone fish doesn't
        linger in the audit trail."""
        if not qualified_names:
            return
        s = self.s
        rm = s.rm
        name_set = set(qualified_names)
        # Delete from the end so earlier indices don't shift mid-loop.
        for i in range(rm.getCount() - 1, -1, -1):
            if rm.getName(i) in name_set:
                rm.select(i)
                rm.runCommand("Delete")
        zip_path = self.exporter.master_zip_path()
        if rm.getCount() > 0:
            rm.deselect()
            rm.runCommand("Save", zip_path)
        elif os.path.exists(zip_path):
            os.remove(zip_path)

    # ---- UI plumbing -----------------------------------------------------

    def _status(self, text):
        if self.panel is not None:
            self.panel.set_status(text)
        log(text)

    def _refresh_panel(self):
        if self.panel is not None:
            self.panel.refresh()

    def shutdown(self):
        Roi.removeRoiListener(self.roi_listener)


# ===========================================================================
#  Floating control panel (non-modal)
# ===========================================================================

class ControlPanel(object):

    def __init__(self, controller):
        self.controller = controller
        self.controller.panel = self
        self.frame = None
        self.status_label = None
        self.mode_label = None
        self.fish_label = None
        self.progress_label = None
        self.lock_label = None
        self.channel_panel = None
        self.root = None
        self._build()

    def _build(self):
        frame = JFrame("Zebrafish Quant")
        frame.setDefaultCloseOperation(JFrame.DO_NOTHING_ON_CLOSE)
        frame.setAlwaysOnTop(True)

        root = JPanel()
        root.setLayout(BoxLayout(root, BoxLayout.Y_AXIS))
        root.setBorder(EmptyBorder(8, 8, 8, 8))
        self.root = root

        self.mode_label = JLabel("")
        self.mode_label.setFont(Font("SansSerif", Font.BOLD, 13))
        self.fish_label = JLabel("")
        self.progress_label = JLabel("")
        self.lock_label = JLabel("")
        self.lock_label.setFont(Font("SansSerif", Font.BOLD, 13))
        self.lock_label.setForeground(Color.RED)
        self.status_label = JLabel("<html><i>Ready.</i></html>")
        self.status_label.setPreferredSize(Dimension(280, 40))

        root.add(self.mode_label)
        root.add(self.fish_label)
        root.add(self.progress_label)
        root.add(self.lock_label)
        root.add(self.status_label)

        root.add(JLabel("Channels (act on whichever image is active now):"))
        self.channel_panel = JPanel(GridLayout(0, 1, 4, 4))
        root.add(self.channel_panel)
        self._rebuild_channel_buttons()

        buttons = JPanel(GridLayout(0, 1, 4, 4))
        buttons.add(self._btn("Add Another Fish",
                              lambda e: self.controller.add_another_fish()))
        buttons.add(self._btn("Skip This Fish (reduce target by 1)",
                              lambda e: self.controller.skip_fish()))
        buttons.add(self._btn("Add Custom Channel",
                              lambda e: self.controller.add_custom_channel()))
        buttons.add(self._btn("Go Back / Modify Previous",
                              lambda e: self.controller.go_back()))
        buttons.add(self._btn("Accept ROI / Next Step (Space)",
                              lambda e: self.controller.next_step()))
        buttons.add(self._btn("Back One Channel / Step (Backspace)",
                              lambda e: self.controller.prev_step()))
        buttons.add(self._btn("Skip Channel / Step (S)",
                              lambda e: self.controller.skip_current()))
        buttons.add(self._btn("Prev Image  [   /   Next Image  ]",
                              lambda e: self.controller.hop_image(1)))
        buttons.add(self._btn("Commit Fish (Enter)",
                              lambda e: self.controller.on_commit_pressed()))
        root.add(buttons)

        frame.getContentPane().add(root, BorderLayout.CENTER)
        # Route keystrokes on the panel through the same hotkey engine.
        frame.addKeyListener(self.controller.key_listener)
        for c in [frame] + list(buttons.getComponents()):
            c.setFocusable(True)
        frame.pack()
        frame.setLocation(40, 80)
        frame.setVisible(True)
        self.frame = frame
        self.refresh()

    def _btn(self, text, handler):
        b = JButton(text)
        b.addActionListener(handler)
        return b

    def _rebuild_channel_buttons(self):
        """One button per configured channel: BF (Eye) + each FL channel.
        Rebuilt whenever the channel list (or its done/not-done state) can
        change, so a checkmark always reflects what's actually captured."""
        s = self.controller.s
        self.channel_panel.removeAll()

        bf_name = s.config.channel_name(s.config.bf_index)
        bf_mark = " [done]" if s.channel_done("Eye") else ""
        self.channel_panel.add(self._btn("[1] %s (Eye)%s" % (bf_name, bf_mark),
                                         lambda e: self.controller.arm_eye()))

        def make_handler(ch):
            return lambda e: self.controller.arm_fl(ch)

        for i, ch in enumerate(s.active_fl_channels):
            mark = " [done]" if s.channel_done(ch["name"]) else ""
            label = "[%d] %s%s" % (i + 2, ch["name"], mark)
            self.channel_panel.add(self._btn(label, make_handler(ch)))

        self.channel_panel.revalidate()
        self.channel_panel.repaint()
        if self.frame is not None:
            self.frame.pack()

    def set_status(self, text):
        def _do():
            self.status_label.setText("<html>%s</html>" % text)
        SwingUtilities.invokeLater(_do)

    def refresh(self):
        s = self.controller.s
        def _do():
            mode_text = "Mode: %s" % s.mode
            if s.finished:
                mode_text += "  [SESSION FINISHED]"
            self.mode_label.setText(mode_text)
            job = s.current_job["label"] if s.current_job else "-"
            self.fish_label.setText(
                "<html>Image: %s<br>Working on Fish %d (session total %d/%d)</html>"
                % (job, s.fish_id, s.fish_committed, s.fish_target))
            done, total = s.channels_progress()
            self.progress_label.setText("Channels done: %d/%d" % (done, total))
            c = self.controller
            if c._manual_phase is not None and c._armed_slice is not None:
                self.lock_label.setText(
                    "LOCKED on slice %d -- do not scroll the stack!"
                    % c._armed_slice)
            else:
                self.lock_label.setText("")
            if self.channel_panel is not None:
                self._rebuild_channel_buttons()
        SwingUtilities.invokeLater(_do)


# ===========================================================================
#  Main
# ===========================================================================

def main():
    config = StartupConfig()
    if not config.prompt():
        log("Setup cancelled.")
        return

    session = ZebrafishSession(config)
    # (session.start() below will error out if no image is currently active.)

    controller = WorkflowController(session)
    panel = ControlPanel(controller)

    # Clean up the RoiListener when the panel closes.
    class _Closer(WindowAdapter):
        def windowClosing(self, e):
            controller.shutdown()
    panel.frame.addWindowListener(_Closer())
    panel.frame.setDefaultCloseOperation(JFrame.DISPOSE_ON_CLOSE)

    controller.start()


if __name__ == "__main__" or True:
    # Fiji's Jython runner executes the file top-to-bottom; guard for both.
    try:
        main()
    except Exception:
        IJ.log(traceback.format_exc())
