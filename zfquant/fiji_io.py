"""The only module that talks to ImageJ.

Everything Java-facing lives here so the rest of the package stays testable
under plain CPython. This file is Jython 2.7 only and cannot be imported
outside Fiji.

Jython traps that bit the legacy script, and one new one, are handled below and
marked with TRAP comments -- they are not obvious from the Java API docs.
"""

from __future__ import division

import os
import traceback

from java.awt import Color, Font

from ij import IJ, ImagePlus, WindowManager
from ij.gui import Overlay, Roi, ShapeRoi, TextRoi, Toolbar
from ij.io import FileSaver
from ij.measure import Measurements
from ij.plugin import Duplicator
from ij.plugin.filter import ThresholdToSelection
from ij.plugin.frame import RoiManager
from ij.process import ByteProcessor, ImageStatistics

from zfquant import core
from zfquant import manifest as manifest_mod


# TRAP: Toolbar.getInstance().WAND reads a static field through an instance and
# can NPE before the toolbar is realised. Use the class constants directly.
TOOL_RECTANGLE = Toolbar.RECTANGLE
TOOL_OVAL = Toolbar.OVAL
TOOL_WAND = Toolbar.WAND

MEASUREMENTS = (Measurements.AREA | Measurements.MEAN | Measurements.STD_DEV |
                Measurements.INTEGRATED_DENSITY | Measurements.MIN_MAX |
                Measurements.MEDIAN | Measurements.MODE |
                Measurements.SKEWNESS | Measurements.KURTOSIS)

# Speck filtering runs the tested pure-Python component pass from core.py, so
# production and the test suite share one implementation rather than drifting.
# It is bounded by the operator's box rather than the frame, but a very large
# box would still be slow under Jython, so past this budget filtering is skipped
# and the row records that it was skipped instead of quietly not happening.
MAX_FILTER_PIXELS = 400000

JPEG_QUALITY = 90


def log(message):
    IJ.log("[ZFQuant] " + str(message))


# ---------------------------------------------------------------------------
#  Images and navigation
# ---------------------------------------------------------------------------

def current_image():
    """Whatever image is frontmost in Fiji *right now*.

    Always called at the moment of an action, never cached. The legacy tool
    kept a `current_imp` reference and read the ROI from it at accept time, so
    drawing on a different window than the one the session last focused
    captured the wrong window's stale selection.
    """
    return WindowManager.getCurrentImage()


def image_by_key(image_key):
    """Resolve a manifest image_key (an ImageJ ID) to an open image."""
    if image_key is None:
        return None
    try:
        return WindowManager.getImage(image_key)
    except Exception:
        return None


def open_images():
    ids = WindowManager.getIDList()
    if ids is None:
        return []
    images = []
    for image_id in ids:
        imp = WindowManager.getImage(image_id)
        if imp is not None:
            images.append(imp)
    return images


def focus(imp):
    if imp is None:
        return None
    window = imp.getWindow()
    if window is not None:
        WindowManager.setCurrentWindow(window)
        window.toFront()
    return imp


def is_open(imp):
    """True if `imp` is still a live, open window.

    An ImagePlus reference stays a valid Python object after the operator
    closes its window, so holding one is not proof it is still usable --
    WindowManager is the source of truth for "still open."
    """
    return imp is not None and WindowManager.getImage(imp.getID()) is not None


def seek_to(plane):
    """Navigate to a recorded plane and return its image.

    The legacy script had two near-identical navigation helpers whose confusion
    caused the recurring "why did it jump" bugs. There is one here, and it is
    driven by an explicit Plane from the manifest rather than by guessing from
    getNChannels() at the call site.
    """
    if plane is None:
        return None
    imp = image_by_key(plane.image_key)
    if imp is None:
        return None
    if plane.slice_index is not None:
        imp.setSlice(max(1, min(plane.slice_index, imp.getStackSize())))
    else:
        channel = max(1, min(plane.channel, imp.getNChannels()))
        z = max(1, min(plane.z or 1, imp.getNSlices()))
        t = max(1, min(plane.t or 1, imp.getNFrames()))
        imp.setPosition(channel, z, t)
    imp.updateAndDraw()
    return imp


def plane_of(imp):
    """The Plane describing where `imp` is displayed right now.

    Used by the "measure the plane I am actually looking at" override, so the
    substituted position is recorded in the same form as a planned one.
    """
    if imp is None:
        return None
    if imp.getNChannels() > 1:
        return manifest_mod.Plane(imp.getID(), imp.getC(), imp.getZ(),
                                  imp.getT(), None, imp.getTitle())
    return manifest_mod.Plane(imp.getID(), 1, 1, 1, imp.getCurrentSlice(),
                              imp.getTitle())


def value_max_for(imp):
    """Highest representable value for this image's bit depth."""
    return core.saturation_value_for(imp.getBitDepth())


def calibration_info(imp):
    """The IMAGE_KEYS provenance that makes rows comparable across files.

    Recording pixel size and unit is what prevents the silent unit mixing the
    legacy CSV allowed, where calibrated and uncalibrated images put um^2 and
    px^2 in the same Area column with no way to tell them apart afterwards.
    """
    calibration = imp.getCalibration()
    calibrated = bool(calibration is not None and
                      calibration.scaled())
    return {
        "PixelWidth": calibration.pixelWidth if calibration else "",
        "PixelUnit": calibration.getUnit() if calibration else "",
        "BitDepth": imp.getBitDepth(),
        "Calibrated": calibrated,
    }


# ---------------------------------------------------------------------------
#  Measurement
# ---------------------------------------------------------------------------

def measure(imp, roi):
    """Full STAT_KEYS statistics for `roi` on the currently displayed plane.

    Both a calibrated and an uncalibrated pass are taken, because IntDen and
    RawIntDen genuinely need different means -- the legacy script used the
    calibrated mean for both and mislabelled the result.
    """
    processor = imp.getProcessor()
    processor.setRoi(roi)
    calibration = imp.getCalibration()

    calibrated = ImageStatistics.getStatistics(processor, MEASUREMENTS,
                                               calibration)
    # Passing no calibration yields raw pixel-value statistics.
    raw = ImageStatistics.getStatistics(processor, MEASUREMENTS, None)

    try:
        roi.setImage(imp)
        perimeter = roi.getLength()
    except Exception:
        perimeter = 0.0

    return core.stats_dict(
        area=calibrated.area,
        mean=calibrated.mean,
        std_dev=calibrated.stdDev,
        median=calibrated.median,
        mode=calibrated.dmode,
        minimum=calibrated.min,
        maximum=calibrated.max,
        perimeter=perimeter,
        pixel_count=raw.pixelCount,
        raw_mean=raw.mean,
        skewness=calibrated.skewness,
        kurtosis=calibrated.kurtosis,
    )


def _stat_field(stats, name, default):
    """Read a field off an ImageStatistics-like object, tolerating Jython
    exposing it as a bound method instead of a plain value.

    TRAP: `stats.histogram` can come back as an unbound/bound Java method
    object rather than the int[] itself. Jython normally turns a zero-arg
    JavaBean getter into a plain attribute automatically, but when a name is
    overloaded (multiple getHistogram(...) signatures) it backs off and just
    hands back the callable under that name instead of invoking it -- so
    `list(stats.histogram)` raises "'instancemethod' object is not iterable"
    rather than returning the array. Detect and call it defensively, since
    binSize/histMin could in principle hit the same trap on a different Fiji
    build even though they haven't so far.
    """
    value = getattr(stats, name, default)
    if callable(value):
        try:
            value = value()
        except Exception:
            return default
    return default if value is None else value


def histogram_of(imp, roi):
    """``(counts, hist_min, bin_width)`` for a region, for percentile fallback."""
    processor = imp.getProcessor()
    processor.setRoi(roi)
    stats = ImageStatistics.getStatistics(processor, MEASUREMENTS, None)
    counts = list(_stat_field(stats, "histogram", []))
    bin_width = _stat_field(stats, "binSize", 1) or 1
    hist_min = _stat_field(stats, "histMin", 0) or 0
    return counts, hist_min, bin_width


def roi_pixels(imp, roi):
    """Raw pixel values inside `roi`, as a plain list.

    Only used for saturation and focus, both of which run on small regions.
    """
    processor = imp.getProcessor()
    bounds = roi.getBounds()
    mask = roi.getMask()
    values = []
    for y in range(bounds.height):
        for x in range(bounds.width):
            if mask is not None and mask.get(x, y) == 0:
                continue
            values.append(processor.getPixelValue(bounds.x + x, bounds.y + y))
    return values


def focus_score(imp, roi):
    """Focus metric over the ROI's bounding box, downsampled if large."""
    processor = imp.getProcessor()
    bounds = roi.getBounds()
    if bounds.width < 3 or bounds.height < 3:
        return None

    step = 1
    while (bounds.width // step) * (bounds.height // step) > 40000:
        step += 1

    width = bounds.width // step
    height = bounds.height // step
    if width < 3 or height < 3:
        return None

    pixels = []
    for y in range(height):
        for x in range(width):
            pixels.append(processor.getPixelValue(bounds.x + x * step,
                                                  bounds.y + y * step))
    return core.variance_of_laplacian(pixels, width, height)


# ---------------------------------------------------------------------------
#  Box-select
# ---------------------------------------------------------------------------

class BoxSelection(object):
    """The outcome of one box-select: the ROI to measure, plus its provenance."""

    def __init__(self, roi, threshold, box, kept=0, rejected=0,
                 signal_pixels=0, filtered=True):
        self.roi = roi
        self.threshold = threshold
        self.box = box
        self.kept = kept
        self.rejected = rejected
        self.signal_pixels = signal_pixels
        self.filtered = filtered

    def provenance(self):
        bounds = self.box.getBounds()
        return {
            "BoxX": bounds.x, "BoxY": bounds.y,
            "BoxW": bounds.width, "BoxH": bounds.height,
            "SignalPixels": self.signal_pixels,
            "ComponentsKept": self.kept,
            "ComponentsRejected": (self.rejected if self.filtered
                                   else "not_filtered"),
        }


def box_select(imp, box_roi, threshold, min_area=core.DEFAULT_MIN_AREA):
    """Select every thresholded pixel inside `box_roi`.

    This is the gesture that replaces the magic wand. The wand traced only the
    single connected blob under the click point, so a punctate signal lost every
    component the operator did not happen to click, and the result depended on
    where inside the signal they clicked.

    Returns a BoxSelection, or None when nothing survives.
    """
    if box_roi is None or threshold is None:
        return None

    processor = imp.getProcessor()
    processor.setRoi(None)
    processor.setThreshold(threshold.low, threshold.high, processor.RED_LUT)

    thresholded = ThresholdToSelection().convert(processor)
    if thresholded is None:
        processor.resetThreshold()
        return None

    # TRAP: ShapeRoi's set operations are named and/or/not/xor, which are Python
    # keywords -- `a.and(b)` is a syntax error in Jython. They have to be reached
    # through getattr.
    intersect = getattr(ShapeRoi(thresholded), "and")
    clipped = intersect(ShapeRoi(box_roi))
    if clipped is None or clipped.getBounds().width == 0:
        processor.resetThreshold()
        return None

    selection, kept, rejected, filtered = _filter_specks(clipped, min_area)
    processor.resetThreshold()
    if selection is None:
        return None

    return BoxSelection(selection, threshold, box_roi, kept=kept,
                        rejected=rejected,
                        signal_pixels=_mask_area(selection),
                        filtered=filtered)


def _filter_specks(roi, min_area):
    """Drop connected components below `min_area`.

    Runs core.select_in_box -- the same code the test suite pins down -- over
    the ROI's own bounding box rather than the frame, so production and tests
    cannot disagree about what box-select means. Past a pixel budget the pass is
    skipped rather than stalling the UI, and the caller records that it was
    skipped.
    """
    if min_area <= 0:
        return roi, 1, 0, True

    bounds = roi.getBounds()
    total = bounds.width * bounds.height
    if total > MAX_FILTER_PIXELS:
        log("Box is %d px; skipping speck filter (budget %d)."
            % (total, MAX_FILTER_PIXELS))
        return roi, 0, 0, False

    mask = roi.getMask()
    if mask is None:
        return roi, 1, 0, True

    flat = []
    for y in range(bounds.height):
        for x in range(bounds.width):
            flat.append(1 if mask.get(x, y) != 0 else 0)

    selected, kept, rejected = core.select_in_box(
        flat, bounds.width, bounds.height,
        (0, 0, bounds.width, bounds.height), min_area=min_area)

    if not kept:
        return None, 0, len(rejected), True

    rebuilt = _roi_from_mask(selected, bounds.width, bounds.height,
                             bounds.x, bounds.y)
    return rebuilt, len(kept), len(rejected), True


def _roi_from_mask(flat, width, height, offset_x, offset_y):
    """Turn a flat 0/1 mask back into an ImageJ Roi at the right position."""
    processor = ByteProcessor(width, height)
    for index, value in enumerate(flat):
        if value:
            processor.set(index % width, index // width, 255)
    processor.setThreshold(128, 255, processor.NO_LUT_UPDATE)
    roi = ThresholdToSelection().convert(processor)
    if roi is None:
        return None
    bounds = roi.getBounds()
    roi.setLocation(bounds.x + offset_x, bounds.y + offset_y)
    return roi


def _mask_area(roi):
    mask = roi.getMask()
    if mask is None:
        bounds = roi.getBounds()
        return bounds.width * bounds.height
    bounds = roi.getBounds()
    count = 0
    for y in range(bounds.height):
        for x in range(bounds.width):
            if mask.get(x, y) != 0:
                count += 1
    return count


def open_threshold_window(imp, threshold):
    """Open (or refocus) ImageJ's own interactive Threshold window, seeded with
    `threshold`, so the operator can see the red overlay and drag the sliders
    to adjust it before drawing the signal box.

    TRAP (part 1): the Threshold window recomputes its own value the moment
    the image or position changes under it. Setting our computed threshold
    BEFORE opening the window gets silently overwritten by the window's own
    default the instant it opens. Opening first, then setting the value, is
    the only ordering that sticks -- same trap the legacy script's
    _apply_threshold already had to work around.

    TRAP (part 2, easy to miss): setting the threshold via
    `ip.setThreshold(...)` only changes the ImageProcessor's own internal
    state -- it does NOT notify an already-open Threshold window, which keeps
    its own separate copy of the slider values. The window re-applies ITS
    (stale/default) copy back onto the image the next time it reacts to an
    image-changed event -- including the imp.updateAndDraw() call right
    below this comment -- silently discarding the value that was just set.
    The static `IJ.setThreshold(imp, low, high)` is the one call that also
    syncs the window's own sliders, not just the processor.

    Display range is snapshotted and restored: thresholding must never drift
    the operator's B&C settings.
    """
    try:
        processor = imp.getProcessor()
        display_min, display_max = processor.getMin(), processor.getMax()

        IJ.run(imp, "Threshold...", "")
        IJ.setThreshold(imp, threshold.low, threshold.high)
        imp.getProcessor().setMinAndMax(display_min, display_max)
        imp.updateAndDraw()
    except Exception:
        log("Failed to open threshold window: " + traceback.format_exc())


def current_threshold(imp):
    """The low/high threshold actually set on `imp` right now, or None.

    Reads back whatever the operator left the Threshold window's sliders at,
    so an on-screen adjustment can actually take effect instead of the tool
    silently re-applying the value it originally computed.
    """
    processor = imp.getProcessor()
    low = processor.getMinThreshold()
    high = processor.getMaxThreshold()
    if low == processor.NO_THRESHOLD or high == processor.NO_THRESHOLD:
        return None
    return low, high


def clear_threshold(imp):
    try:
        processor = imp.getProcessor()
        display_min, display_max = processor.getMin(), processor.getMax()
        processor.resetThreshold()
        processor.setMinAndMax(display_min, display_max)
        imp.updateAndDraw()
    except Exception:
        log("Clear threshold failed: " + traceback.format_exc())


def arm_tool(tool_id):
    Toolbar.getInstance().setTool(tool_id)


# ---------------------------------------------------------------------------
#  Export
# ---------------------------------------------------------------------------

class RoiArchive(object):
    """The single session-wide ROI archive, appended to rather than rebuilt."""

    def __init__(self, zip_path):
        self.zip_path = zip_path
        self.manager = RoiManager.getInstance()
        if self.manager is None:
            self.manager = RoiManager()
        # Deliberately NOT reset: a resumed session must not blank the archive
        # from a previous run, and the operator should keep seeing the ROIs they
        # just saved as confirmation.
        self._pending = 0

    def add(self, roi, name, plane):
        roi = roi.clone()
        roi.setName(name)
        if plane is not None:
            if plane.slice_index is not None:
                roi.setPosition(plane.slice_index)
            else:
                roi.setPosition(plane.channel, plane.z or 1, plane.t or 1)
        self.manager.addRoi(roi)
        self._pending += 1
        return name

    def flush(self):
        """Write the archive out.

        Still a full write each time -- ImageJ's RoiManager has no
        append-to-zip -- called synchronously right after each channel is
        accepted (not batched to fish-commit time) so a crash mid-fish loses
        at most the channel still in progress. That trades a small amount of
        per-accept latency, proportional to the archive's total size, for that
        safety; if it turns out to be a noticeable pause in practice this can
        move to a background thread the same way commit() already does for
        the CSV/journal write.
        """
        if self.manager.getCount() == 0:
            return None
        self.manager.deselect()
        self.manager.runCommand("Save", self.zip_path)
        self._pending = 0
        return self.zip_path

    def remove_named(self, names):
        if not names:
            return 0
        wanted = set(names)
        removed = 0
        # Backwards, so earlier indices do not shift mid-loop.
        for index in range(self.manager.getCount() - 1, -1, -1):
            if self.manager.getName(index) in wanted:
                self.manager.select(index)
                self.manager.runCommand("Delete")
                removed += 1
        self.manager.deselect()
        if self.manager.getCount() > 0:
            self.manager.runCommand("Save", self.zip_path)
        elif os.path.exists(self.zip_path):
            os.remove(self.zip_path)
        return removed


WORKING_IMAGE_PROPERTY = "zfquant.working"

# Roughly beside the control panel (which sits at 40,80), so the working image
# never opens on top of it.
WORKING_WINDOW_LOCATION = (420, 80)


def duplicate_plane(imp, plane, title):
    """Duplicate exactly one plane of `imp` into a new, unshown ImagePlus.

    Shared by the interactive working copy and the audit-image exporter, so
    there is exactly one place that knows how to pull a single plane out of a
    stack -- the legacy exporter duplicated the ENTIRE stack (all channels, all
    Z, all T) just to flatten one plane, which on a large hyperstack was
    gigabytes of copying per fish and a realistic OutOfMemoryError.

    `imp` is the SOURCE (often the operator's own live stack, not a working
    copy) and Duplicator crops to whatever ROI is currently active on it --
    silently, with no error -- rather than duplicating the whole plane. A
    selection can be left there by perfectly ordinary use (the operator
    clicking around the real stack, use_current_plane's redirect, a box
    still active when undo tears down the working copy mid-draw, ...); with
    no guard, the very next working copy opened from this source is cropped
    down to that leftover selection instead of showing the whole plane.
    Clearing it here, once, is what makes duplication always return the
    whole plane regardless of how a selection got left behind upstream.
    """
    imp.deleteRoi()
    if plane.slice_index is not None:
        single = Duplicator().run(imp, plane.slice_index, plane.slice_index)
    else:
        channel = plane.channel
        z = plane.z or 1
        t = plane.t or 1
        single = Duplicator().run(imp, channel, channel, z, z, t, t)
    single.setTitle(title)
    return single


def is_working_image(imp):
    """True only for a duplicate created by open_working_copy.

    Lets navigation helpers (use_current_plane, the "pick an image" fallback in
    Controller.arm) tell a real operator window apart from the tool's own
    scratch copy, so a redirect can't accidentally target itself.
    """
    return imp is not None and imp.getProperty(WORKING_IMAGE_PROPERTY) == "1"


def open_working_copy(imp, plane, title):
    """Show a single-plane duplicate for the operator to draw on directly.

    This is what makes the "stack scrolled away mid-draw" bug class impossible
    rather than merely detected: the operator's ellipse/box is drawn on this
    small standalone window, which has no stack dimension at all, so there is
    nothing left to scroll it away from. Modelled on a lab macro that duplicates
    the current slice before every measurement step for exactly this reason.
    """
    working = duplicate_plane(imp, plane, title)
    working.setProperty(WORKING_IMAGE_PROPERTY, "1")
    working.show()
    window = working.getWindow()
    if window is not None:
        window.setLocation(*WORKING_WINDOW_LOCATION)
        window.toFront()
    return working


def close_working_copy(imp):
    if imp is None or not is_working_image(imp):
        return
    try:
        imp.changes = False   # suppress "save changes?" on close
        imp.close()
    except Exception:
        log("Failed to close working copy: " + traceback.format_exc())


# ---------------------------------------------------------------------------
#  Review-time position labeling
# ---------------------------------------------------------------------------
#
# Burns the current review label(s) (e.g. "1b" or "1b 2b") into the corner of
# the image being reviewed, so the operator can see the assignment directly
# against the image rather than only reading it off a separate panel.

_LABEL_FONT = Font("SansSerif", Font.BOLD, 18)
_LABEL_TEXT_COLOR = Color.YELLOW
_LABEL_BACKGROUND_COLOR = Color(0, 0, 0, 160)   # translucent, readable on any plane
_LABEL_MARGIN = 6


def set_position_label(imp, text):
    """Replace imp's Overlay with a single text label at bottom-left, in
    image coordinates (so it tracks the image, not the window).

    Idempotent: safe to call every time the review's current position or
    fish list changes -- replaces rather than accumulates, and a filled
    background rectangle behind the text keeps it legible on both a bright
    brightfield plane and a mostly-dark fluorescence one.
    """
    if imp is None or not text:
        return
    try:
        width = _text_width(text)
        height = _LABEL_FONT.getSize() + _LABEL_MARGIN
        x = _LABEL_MARGIN
        y = max(0, imp.getHeight() - height - _LABEL_MARGIN)

        background = Roi(x, y, width + 2 * _LABEL_MARGIN, height + _LABEL_MARGIN)
        background.setFillColor(_LABEL_BACKGROUND_COLOR)

        label = TextRoi(x + _LABEL_MARGIN, y + _LABEL_MARGIN // 2, text,
                        _LABEL_FONT)
        label.setStrokeColor(_LABEL_TEXT_COLOR)

        overlay = Overlay()
        overlay.add(background)
        overlay.add(label)
        imp.setOverlay(overlay)
        imp.updateAndDraw()
        canvas = imp.getCanvas()
        if canvas is not None:
            canvas.repaint()
    except Exception:
        log("Failed to draw position label: " + traceback.format_exc())


def clear_position_label(imp):
    """Remove any review label from `imp`.

    Overlay is a property of the ImagePlus, not the current slice/position --
    it survives seek_to() navigation and a Manual-mode window hop, so this has
    to be called explicitly both when review finishes and whenever review
    moves off an image it previously labeled, or a stale label lingers into
    the measurement phase (and would otherwise show through in any later
    export_audit_image() overlay built on the same image).
    """
    if imp is None:
        return
    try:
        imp.setOverlay(None)
        imp.updateAndDraw()
    except Exception:
        log("Failed to clear position label: " + traceback.format_exc())


def _text_width(text):
    """Rough pixel width of `text` at _LABEL_FONT, for sizing the background
    rectangle. FontMetrics needs a Graphics context; a plain BufferedImage
    gives us one without putting anything on screen."""
    from java.awt.image import BufferedImage
    probe = BufferedImage(1, 1, BufferedImage.TYPE_INT_ARGB)
    graphics = probe.createGraphics()
    try:
        return graphics.getFontMetrics(_LABEL_FONT).stringWidth(text)
    finally:
        graphics.dispose()


def export_audit_image(plane, roi_color_pairs, out_path):
    """One audit image per channel, showing exactly what was measured.

    Deliberately plain fluorescence -- no threshold overlay painted over it.
    The signal/box/background outlines already show what was measured; a red
    threshold wash on top of that only obscures the raw image the operator
    would actually want to eyeball (exposure, focus, general signal quality).
    The threshold VALUE itself is not lost -- it is written into the CSV as
    provenance (see core.PROVENANCE_KEYS / ThresholdResult.as_row_fields()).
    """
    imp = seek_to(plane)
    if imp is None:
        return None

    single = duplicate_plane(imp, plane, "audit")
    try:
        overlay = Overlay()
        for roi, color in roi_color_pairs:
            outlined = roi.clone()
            outlined.setStrokeColor(color)
            outlined.setStrokeWidth(2)
            overlay.add(outlined)
        single.setOverlay(overlay)

        flat = single.flatten()
        try:
            if out_path.lower().endswith(".png"):
                FileSaver(flat).saveAsPng(out_path)
            else:
                # TRAP: IJ.run(imp, "Set JPEG Quality...", ...) opens a dialog
                # and can throw "Macro canceled". The static setter does not.
                FileSaver.setJpegQuality(JPEG_QUALITY)
                FileSaver(flat).saveAsJpeg(out_path)
        finally:
            flat.close()
    finally:
        single.close()
    return out_path


EYE_COLOR = Color.YELLOW
SIGNAL_COLOR = Color.GREEN
BACKGROUND_COLOR = Color.CYAN
BOX_COLOR = Color.MAGENTA
