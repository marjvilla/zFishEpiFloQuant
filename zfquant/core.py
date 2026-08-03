"""Pure measurement and threshold logic.

CRITICAL CONSTRAINT: this module must import and run under BOTH Jython 2.7
(inside Fiji) and CPython 3 (for the test suite). Therefore:

  * no ``ij.*`` imports and no Java types anywhere in this file
  * Python 2/3 compatible syntax only -- no f-strings, no annotations
  * no numpy; Jython does not have it

Everything here takes plain numbers, dicts and sequences. All ImageJ work lives
in ``zfquant/fiji_io.py``.

Why this split matters: the corrected-intensity math is the entire point of the
tool and in the legacy script it was never verifiable, because every code path
needed a live ImageJ. Here it is ordinary arithmetic over plain data, so it can
be pinned down by tests with known ground truth.
"""

from __future__ import division, print_function

import math


# ---------------------------------------------------------------------------
#  Measurement schema
# ---------------------------------------------------------------------------

# Every raw statistic saved per ROI. Kept as a single list so the CSV header and
# the row builder cannot drift apart -- that decision from the legacy script was
# right and is preserved.
STAT_KEYS = ["Area", "Mean", "StdDev", "Median", "Mode", "Min", "Max",
             "Perimeter", "IntDen", "RawIntDen", "Skewness", "Kurtosis"]

# Provenance recorded per fluorescent channel: the analytical decisions that
# produced the numbers. The legacy tool recorded none of these, which made its
# output impossible to reproduce -- the operator dragged a threshold slider and
# that value was never written anywhere.
PROVENANCE_KEYS = ["ThresholdLow", "ThresholdHigh", "ThresholdMethod",
                   "ThresholdK", "ThresholdOverridden", "MinArea",
                   "BoxX", "BoxY", "BoxW", "BoxH",
                   "SignalPixels", "ComponentsKept", "ComponentsRejected",
                   "SaturatedFraction"]

# Provenance recorded once per fish (image-level, not channel-level).
IMAGE_KEYS = ["PixelWidth", "PixelUnit", "BitDepth", "Calibrated",
              "FocusScore", "PlaneOverride", "PlaneRecorded", "PlaneExpected",
              "Operator", "MeasuredAt"]

# Threshold methods.
THRESH_BACKGROUND = "background_sd"   # mean_bg + k*sd_bg  (preferred)
THRESH_PERCENTILE = "percentile"      # fallback when the BG box is unusable
THRESH_MANUAL = "manual"              # operator overrode it

DEFAULT_K = 3.0
DEFAULT_PERCENTILE = 99.5
DEFAULT_MIN_AREA = 4

# Sentinel written to the CSV when the operator deliberately skipped something,
# so "not measured on purpose" is distinguishable from "measurement missing".
SKIPPED = "SKIPPED"


class ThresholdResult(object):
    """The threshold actually applied, plus enough context to reproduce it."""

    def __init__(self, low, high, method, k=None, mean_bg=None, sd_bg=None,
                 overridden=False):
        self.low = low
        self.high = high
        self.method = method
        self.k = k
        self.mean_bg = mean_bg
        self.sd_bg = sd_bg
        self.overridden = overridden

    def as_row_fields(self):
        return {"ThresholdLow": self.low,
                "ThresholdHigh": self.high,
                "ThresholdMethod": self.method,
                "ThresholdK": "" if self.k is None else self.k,
                "ThresholdOverridden": bool(self.overridden)}

    def __repr__(self):
        return ("ThresholdResult(low=%r, high=%r, method=%r, k=%r, "
                "overridden=%r)" % (self.low, self.high, self.method, self.k,
                                    self.overridden))


class Component(object):
    """One connected run of thresholded pixels inside the selection box."""

    def __init__(self, area, x0, y0, x1, y1):
        self.area = area
        self.bounds = (x0, y0, x1 - x0 + 1, y1 - y0 + 1)   # x, y, w, h

    def __repr__(self):
        return "Component(area=%d, bounds=%r)" % (self.area, self.bounds)


# ---------------------------------------------------------------------------
#  Thresholding
# ---------------------------------------------------------------------------

def threshold_from_background(mean_bg, sd_bg, value_max, k=DEFAULT_K):
    """Background-referenced cutoff: ``mean_bg + k*sd_bg``.

    This replaces the legacy ``min + 0.95*(max - min)`` rule, whose cutoff was
    set by the single brightest pixel in the frame. One hot pixel, dust speck or
    specular reflection moved the threshold and silently excluded real signal;
    worse, because the cutoff was re-derived per slice from that slice's own
    extremes, the same biological signal produced a different Area on every
    image and corrected values were not comparable between fish.

    Referencing the cutoff to the background the operator has already drawn
    makes it insensitive to bright outliers elsewhere in the frame and puts every
    fish on the same footing: "signal is what exceeds this sample's own noise
    floor by k standard deviations".

    Returns None when the background box cannot support a threshold (a
    degenerate sd, which happens on synthetic or heavily clipped regions); the
    caller is expected to fall back to `percentile_from_histogram`.
    """
    if sd_bg is None or mean_bg is None:
        return None
    if sd_bg <= 0 or _is_nan(sd_bg) or _is_nan(mean_bg):
        return None
    low = mean_bg + k * sd_bg
    if low >= value_max:
        # Threshold at or above the top of the range selects nothing. Refuse
        # rather than hand back a cutoff that yields an empty ROI and a
        # silently-zero measurement.
        return None
    return ThresholdResult(low, value_max, THRESH_BACKGROUND, k=k,
                           mean_bg=mean_bg, sd_bg=sd_bg)


def percentile_from_histogram(counts, hist_min, bin_width, percentile):
    """Intensity below which `percentile` percent of the pixels fall.

    Used as the fallback threshold when the background box is unusable. Operates
    on a histogram rather than a pixel list because that is what ImageJ hands
    back cheaply, and because a histogram is small enough to walk in pure Python
    even under Jython.
    """
    total = 0
    for c in counts:
        total += c
    if total <= 0:
        return None
    target = total * (percentile / 100.0)
    seen = 0
    for i, c in enumerate(counts):
        seen += c
        if seen >= target:
            return hist_min + (i + 1) * bin_width
    return hist_min + len(counts) * bin_width


def resolve_threshold(bg_stats, hist=None, k=DEFAULT_K,
                      percentile=DEFAULT_PERCENTILE, value_max=None,
                      manual_low=None):
    """Pick the threshold for one channel, preferring the background-referenced
    rule and falling back to a percentile of the signal box.

    `bg_stats` is a stats dict for the background ROI (needs Mean and StdDev),
    or None if no background was drawn. `hist` is
    ``(counts, hist_min, bin_width)`` for the signal box.

    An explicit `manual_low` always wins and is recorded as such, so an operator
    override is visible in the data rather than indistinguishable from an
    automatic result.
    """
    if value_max is None:
        raise ValueError("value_max is required to bound the threshold")

    if manual_low is not None:
        return ThresholdResult(manual_low, value_max, THRESH_MANUAL,
                               overridden=True)

    if bg_stats:
        result = threshold_from_background(bg_stats.get("Mean"),
                                           bg_stats.get("StdDev"),
                                           value_max, k=k)
        if result is not None:
            return result

    if hist:
        counts, hist_min, bin_width = hist
        low = percentile_from_histogram(counts, hist_min, bin_width, percentile)
        if low is not None and low < value_max:
            return ThresholdResult(low, value_max, THRESH_PERCENTILE, k=None)

    return None


# ---------------------------------------------------------------------------
#  Box-select geometry
# ---------------------------------------------------------------------------

def select_in_box(mask, width, height, box, min_area=DEFAULT_MIN_AREA,
                  connectivity=8):
    """All thresholded pixels inside `box`, minus specks.

    This is the semantics of the box-select gesture: the operator drags a box,
    and everything above threshold inside it is selected -- as opposed to the
    legacy magic wand, which traced only the single connected blob under the
    click point and silently dropped every other component of a punctate signal.

    `mask` is a flat sequence of truthy/falsy values, row-major, ``width*height``
    long. `box` is ``(x, y, w, h)``.

    Clipping happens BEFORE the size filter, so a blob straddling the box edge
    is judged on the area that lies inside the box. That matches the ImageJ fast
    path in fiji_io (``ShapeRoi(thresholded).and(ShapeRoi(box))``, then filter),
    and it is the behaviour the operator sees: what is inside the box is what
    gets measured.

    Returns ``(selected, kept, rejected)`` -- a new flat mask of surviving
    pixels, and the `Component` lists on each side of the `min_area` cut.
    """
    bx, by, bw, bh = box
    x0 = max(0, bx)
    y0 = max(0, by)
    x1 = min(width, bx + bw)      # exclusive
    y1 = min(height, by + bh)     # exclusive

    selected = [0] * (width * height)
    kept = []
    rejected = []
    if x1 <= x0 or y1 <= y0:
        return selected, kept, rejected

    if connectivity == 8:
        neighbours = ((-1, -1), (0, -1), (1, -1), (-1, 0),
                      (1, 0), (-1, 1), (0, 1), (1, 1))
    else:
        neighbours = ((0, -1), (-1, 0), (1, 0), (0, 1))

    visited = [False] * (width * height)

    for sy in range(y0, y1):
        row = sy * width
        for sx in range(x0, x1):
            start = row + sx
            if visited[start] or not mask[start]:
                continue

            # Iterative flood fill -- an explicit stack, because a large blob
            # would blow Python's recursion limit.
            stack = [start]
            visited[start] = True
            pixels = []
            min_x = max_x = sx
            min_y = max_y = sy

            while stack:
                idx = stack.pop()
                py, px = divmod(idx, width)
                pixels.append(idx)
                if px < min_x:
                    min_x = px
                if px > max_x:
                    max_x = px
                if py < min_y:
                    min_y = py
                if py > max_y:
                    max_y = py

                for dx, dy in neighbours:
                    nx = px + dx
                    ny = py + dy
                    if nx < x0 or nx >= x1 or ny < y0 or ny >= y1:
                        continue   # outside the box: clipped away
                    nidx = ny * width + nx
                    if visited[nidx] or not mask[nidx]:
                        continue
                    visited[nidx] = True
                    stack.append(nidx)

            comp = Component(len(pixels), min_x, min_y, max_x, max_y)
            if comp.area >= min_area:
                kept.append(comp)
                for idx in pixels:
                    selected[idx] = 1
            else:
                rejected.append(comp)

    return selected, kept, rejected


def mask_from_threshold(pixels, low, high):
    """Flat 0/1 mask of pixels within ``[low, high]``, inclusive both ends."""
    return [1 if (low <= v <= high) else 0 for v in pixels]


# ---------------------------------------------------------------------------
#  Statistics and derived values
# ---------------------------------------------------------------------------

def integrated_density(area, mean):
    """Calibrated integrated density: spatially-calibrated area x mean."""
    return area * mean


def raw_integrated_density(pixel_count, raw_mean):
    """Uncalibrated integrated density: the plain sum of raw pixel values.

    `raw_mean` must be the UNCALIBRATED mean. The legacy script used the
    calibrated mean here and labelled the result "uncalibrated"; on an image
    carrying a density calibration that produced a number which was neither
    ImageJ's RawIntDen nor a raw sum. The two means are only interchangeable on
    images with no intensity calibration, which is exactly the case where nobody
    notices the bug.
    """
    return pixel_count * raw_mean


def corrected_intensity(fl_intden, mean_bg, fl_area):
    """``FL IntDen - (Mean BG x FL Area)`` -- the value the experiment is after.

    Both terms must be in the same units: `fl_intden` calibrated implies
    `fl_area` calibrated. `stats_dict` keeps them consistent by construction.
    """
    return fl_intden - (mean_bg * fl_area)


def stats_dict(area, mean, std_dev, median, mode, minimum, maximum,
               perimeter, pixel_count, raw_mean, skewness, kurtosis):
    """Assemble one ROI's statistics into the STAT_KEYS schema."""
    return {
        "Area": area,
        "Mean": mean,
        "StdDev": std_dev,
        "Median": median,
        "Mode": mode,
        "Min": minimum,
        "Max": maximum,
        "Perimeter": perimeter,
        "IntDen": integrated_density(area, mean),
        "RawIntDen": raw_integrated_density(pixel_count, raw_mean),
        "Skewness": skewness,
        "Kurtosis": kurtosis,
    }


def saturation_value_for(bit_depth):
    """Highest representable value for a bit depth, or None if unknown.

    16-bit cameras very often deliver 12-bit data, in which case the real
    ceiling is 4095 and this returns the wrong answer. That is why the caller
    can pass an explicit ceiling: guessing from the container is not reliable.
    """
    if bit_depth == 8:
        return 255
    if bit_depth == 16:
        return 65535
    return None


def saturated_fraction(pixels, sat_value):
    """Fraction of `pixels` sitting at the ceiling.

    A saturated ROI is not a bright ROI: intensity is clipped, so Mean and
    IntDen understate the truth by an unknown amount and the measurement is not
    quantitative. The legacy CSV had no way to express this -- Max alone cannot
    distinguish "bright" from "clipped" without knowing the bit depth -- so
    saturated fish were silently averaged in with valid ones.
    """
    if sat_value is None:
        return None
    total = 0
    hits = 0
    for v in pixels:
        total += 1
        if v >= sat_value:
            hits += 1
    if total == 0:
        return 0.0
    return hits / total


def variance_of_laplacian(pixels, width, height, normalize=True):
    """Focus score: variance of the 3x3 Laplacian response.

    An out-of-focus fish is dimmer, blurrier and larger at threshold than an
    in-focus one, and in the legacy CSV it was indistinguishable from a
    genuinely dim fish -- so focus drift could not be filtered out after the
    fact. This makes it a column.

    Normalising by mean intensity squared keeps the score comparable between
    fish imaged at different exposures; the raw variance would mostly track
    brightness. Intended for a small patch (an eye ROI bounding box), not a full
    frame -- pure Python over 4 megapixels under Jython would be far too slow.
    """
    if width < 3 or height < 3:
        return None

    responses = []
    total = 0.0
    for y in range(1, height - 1):
        row = y * width
        for x in range(1, width - 1):
            i = row + x
            centre = pixels[i]
            lap = (pixels[i - width] + pixels[i + width] +
                   pixels[i - 1] + pixels[i + 1] - 4 * centre)
            responses.append(lap)
            total += centre

    n = len(responses)
    if n == 0:
        return None

    mean_r = sum(responses) / n
    var = sum((r - mean_r) ** 2 for r in responses) / n

    if not normalize:
        return var
    mean_intensity = total / n
    if mean_intensity <= 0:
        return None
    return var / (mean_intensity ** 2)


# ---------------------------------------------------------------------------
#  Row and header construction
# ---------------------------------------------------------------------------

def csv_header(fl_channel_names):
    """Column order for the session CSV.

    Generated from the same lists the row builder uses, so header and rows
    cannot drift apart.
    """
    header = ["FileName", "FishID"]
    header += IMAGE_KEYS
    header += ["Eye%s" % k for k in STAT_KEYS]
    for name in fl_channel_names:
        header += ["%s_%s" % (name, k) for k in STAT_KEYS]      # signal
        header += ["%s_BG%s" % (name, k) for k in STAT_KEYS]    # background
        header += ["%s_Corrected" % name]                       # derived
        header += ["%s_%s" % (name, k) for k in PROVENANCE_KEYS]
    return header


def build_row(file_name, fish_id, fl_channel_names, eye_stats=None,
              channel_results=None, image_info=None, round_to=4):
    """Assemble one fish's CSV row.

    `channel_results` maps channel name -> dict with keys:
        ``signal``     stats dict for the signal ROI (or None / SKIPPED)
        ``background`` stats dict for the background ROI (or None / SKIPPED)
        ``threshold``  a ThresholdResult (or None)
        ``provenance`` extra PROVENANCE_KEYS values (box geometry, counts, ...)

    Anything the operator deliberately skipped should be passed as the module's
    SKIPPED sentinel rather than None, so the CSV distinguishes "not present in
    this fish" from "measurement failed".
    """
    channel_results = channel_results or {}
    image_info = image_info or {}

    row = {"FileName": file_name, "FishID": fish_id}

    for key in IMAGE_KEYS:
        row[key] = image_info.get(key, "")

    _fill_stats(row, "Eye", eye_stats, round_to)

    for name in fl_channel_names:
        result = channel_results.get(name) or {}
        signal = result.get("signal")
        background = result.get("background")

        # Column names must match csv_header() exactly: "GFP_Area" for signal,
        # "GFP_BGArea" for background, "EyeArea" for the eye.
        _fill_stats(row, name, signal, round_to, joiner="_")
        _fill_stats(row, name + "_BG", background, round_to, joiner="")

        # Corrected intensity needs real numbers on both sides. With no usable
        # background the correction term is zero, which is a meaningfully
        # different claim from "we subtracted a measured background" -- so the
        # background columns stay blank and the reader can tell.
        if _is_stats(signal):
            mean_bg = background["Mean"] if _is_stats(background) else 0.0
            row["%s_Corrected" % name] = _round(
                corrected_intensity(signal["IntDen"], mean_bg, signal["Area"]),
                round_to)
        elif signal == SKIPPED:
            row["%s_Corrected" % name] = SKIPPED
        else:
            row["%s_Corrected" % name] = ""

        threshold = result.get("threshold")
        provenance = dict(result.get("provenance") or {})
        if threshold is not None:
            provenance.update(threshold.as_row_fields())
        for key in PROVENANCE_KEYS:
            row["%s_%s" % (name, key)] = provenance.get(key, "")

    return row


def _fill_stats(row, prefix, stats, round_to, joiner=""):
    """Write one STAT_KEYS block into `row` under `prefix`."""
    for key in STAT_KEYS:
        column = "%s%s%s" % (prefix, joiner, key)
        if _is_stats(stats):
            row[column] = _round(stats.get(key), round_to)
        elif stats == SKIPPED:
            row[column] = SKIPPED
        else:
            row[column] = ""


def _is_stats(value):
    return isinstance(value, dict)


def _round(value, digits):
    """Round half away from zero, identically under Jython 2.7 and CPython 3.

    The builtin cannot be used here: Python 2 rounds half away from zero while
    Python 3 rounds half to even, so ``round(0.5)`` is 1.0 under Jython and 0
    under CPython. That would make measurements taken in Fiji disagree in the
    last decimal with the same computation in the test suite, and would put
    noise into any old-vs-new CSV comparison. Pinning the rule here keeps the
    two environments byte-identical.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    try:
        if _is_nan(value):
            return ""
        if _is_inf(value):
            return value
        factor = 10.0 ** digits
        scaled = value * factor
        if scaled >= 0:
            return math.floor(scaled + 0.5) / factor
        return math.ceil(scaled - 0.5) / factor
    except (TypeError, OverflowError):
        return value


def _is_nan(value):
    try:
        return math.isnan(value)
    except (TypeError, ValueError):
        return False


def _is_inf(value):
    try:
        return math.isinf(value)
    except (TypeError, ValueError):
        return False
