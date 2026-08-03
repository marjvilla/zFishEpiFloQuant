# -*- coding: utf-8 -*-
"""Tests for zfquant.core -- the math the legacy tool never verified.

Run with no dependencies:

    python3 -m unittest discover -s tests -v

These are plain unittest TestCases so they need nothing installed, but pytest
will collect them too if it is available.
"""

from __future__ import division, print_function

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zfquant import core   # noqa: E402


def make_frame(width, height, background, blobs):
    """Flat pixel list: uniform `background` with rectangular `blobs` painted on.

    `blobs` is a list of ``(x, y, w, h, value)``.
    """
    pixels = [background] * (width * height)
    for x, y, w, h, value in blobs:
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                pixels[yy * width + xx] = value
    return pixels


class TestCorrectedIntensity(unittest.TestCase):
    """Verification item 1: known ground truth, calibrated and uncalibrated."""

    def test_uncalibrated_ground_truth(self):
        # A 10x10 signal region of value 100 on a background of 10.
        # Area = 100 px, Mean = 100, IntDen = 10000.
        # Corrected = 10000 - (10 * 100) = 9000.
        signal = core.stats_dict(area=100, mean=100.0, std_dev=0.0, median=100,
                                 mode=100, minimum=100, maximum=100,
                                 perimeter=40.0, pixel_count=100,
                                 raw_mean=100.0, skewness=0.0, kurtosis=0.0)
        self.assertEqual(signal["IntDen"], 10000.0)
        self.assertEqual(signal["RawIntDen"], 10000.0)
        self.assertEqual(
            core.corrected_intensity(signal["IntDen"], 10.0, signal["Area"]),
            9000.0)

    def test_calibrated_area_scales_intden_and_correction(self):
        # 0.5 um/px => each pixel is 0.25 um^2, so 100 px = 25 um^2.
        # IntDen = 25 * 100 = 2500; corrected = 2500 - (10 * 25) = 2250.
        signal = core.stats_dict(area=25.0, mean=100.0, std_dev=0.0, median=100,
                                 mode=100, minimum=100, maximum=100,
                                 perimeter=20.0, pixel_count=100,
                                 raw_mean=100.0, skewness=0.0, kurtosis=0.0)
        self.assertEqual(signal["IntDen"], 2500.0)
        self.assertEqual(
            core.corrected_intensity(signal["IntDen"], 10.0, signal["Area"]),
            2250.0)

    def test_rawintden_uses_the_uncalibrated_mean(self):
        """The legacy bug: RawIntDen computed from the calibrated mean.

        With a density calibration of 2x, the calibrated mean is 200 while the
        raw mean is 100. RawIntDen must follow the raw values -- pixel_count *
        raw_mean -- not pixel_count * calibrated_mean.
        """
        stats = core.stats_dict(area=100, mean=200.0, std_dev=0.0, median=200,
                                mode=200, minimum=200, maximum=200,
                                perimeter=40.0, pixel_count=100,
                                raw_mean=100.0, skewness=0.0, kurtosis=0.0)
        self.assertEqual(stats["IntDen"], 20000.0)     # calibrated
        self.assertEqual(stats["RawIntDen"], 10000.0)  # raw, and different
        self.assertNotEqual(stats["IntDen"], stats["RawIntDen"])


class TestThreshold(unittest.TestCase):

    def test_background_referenced_cutoff(self):
        result = core.threshold_from_background(mean_bg=10.0, sd_bg=2.0,
                                                value_max=255, k=3.0)
        self.assertEqual(result.low, 16.0)
        self.assertEqual(result.high, 255)
        self.assertEqual(result.method, core.THRESH_BACKGROUND)
        self.assertFalse(result.overridden)

    def test_hot_pixel_does_not_move_the_threshold(self):
        """Verification item 1's regression case.

        The legacy rule was min + 0.95*(max - min), so a single saturated pixel
        anywhere in the frame dragged the cutoff up and excluded real signal.
        The background-referenced rule cannot see that pixel at all.
        """
        # Two frames with identical background statistics and identical real
        # signal at intensity 500. The only difference is one hot pixel, which
        # raises the frame maximum from 500 to 65535.
        clean = core.threshold_from_background(10.0, 2.0, value_max=500, k=3.0)
        hot = core.threshold_from_background(10.0, 2.0, value_max=65535, k=3.0)

        # The cutoff is unchanged: the frame maximum bounds the threshold but
        # does not set it, so the hot pixel cannot exclude real signal.
        self.assertEqual(clean.low, 16.0)
        self.assertEqual(hot.low, 16.0)
        self.assertLess(clean.low, 500)          # real signal still selected
        self.assertLess(hot.low, 500)

        # For contrast, the legacy formula on the same two frames. The hot pixel
        # pushes the cutoff above the real signal, which then vanishes from the
        # measurement with no error and no warning.
        legacy_clean = 0 + 0.95 * (500 - 0)
        legacy_hot = 0 + 0.95 * (65535 - 0)
        self.assertLess(legacy_clean, 500)       # signal selected
        self.assertGreater(legacy_hot, 500)      # signal silently excluded

    def test_degenerate_background_is_refused_not_guessed(self):
        self.assertIsNone(
            core.threshold_from_background(10.0, 0.0, value_max=255))
        self.assertIsNone(
            core.threshold_from_background(10.0, None, value_max=255))

    def test_cutoff_above_range_is_refused(self):
        # mean 200 + 3*30 = 290 > 255: would select nothing, so refuse rather
        # than hand back a silently-empty ROI.
        self.assertIsNone(
            core.threshold_from_background(200.0, 30.0, value_max=255))

    def test_falls_back_to_percentile_when_background_unusable(self):
        # 1000 pixels: 990 at intensity 10, 10 at intensity 200.
        counts = [0] * 256
        counts[10] = 990
        counts[200] = 10
        result = core.resolve_threshold(
            bg_stats={"Mean": 10.0, "StdDev": 0.0},   # degenerate
            hist=(counts, 0, 1), value_max=255, percentile=99.0)
        self.assertEqual(result.method, core.THRESH_PERCENTILE)
        self.assertGreater(result.low, 10)

    def test_manual_override_wins_and_is_recorded(self):
        result = core.resolve_threshold(
            bg_stats={"Mean": 10.0, "StdDev": 2.0},
            value_max=255, manual_low=42.0)
        self.assertEqual(result.low, 42.0)
        self.assertEqual(result.method, core.THRESH_MANUAL)
        self.assertTrue(result.overridden)
        self.assertTrue(result.as_row_fields()["ThresholdOverridden"])

    def test_percentile_of_histogram(self):
        counts = [0] * 100
        for i in range(100):
            counts[i] = 1          # uniform 0..99, one pixel each
        self.assertAlmostEqual(
            core.percentile_from_histogram(counts, 0, 1, 50), 50.0)


class TestBoxSelect(unittest.TestCase):
    """Verification item 2: box-select clipping geometry."""

    WIDTH = 40
    HEIGHT = 40

    def build(self):
        """Three blobs: two inside the box, one outside, plus a speck inside."""
        pixels = make_frame(self.WIDTH, self.HEIGHT, background=10, blobs=[
            (5, 5, 4, 4, 200),      # blob A: 16 px, inside the box
            (14, 6, 3, 3, 200),     # blob B: 9 px, inside the box
            (30, 30, 5, 5, 200),    # blob C: 25 px, OUTSIDE the box
            (10, 15, 1, 1, 200),    # speck: 1 px, inside the box
        ])
        mask = core.mask_from_threshold(pixels, 100, 255)
        return mask

    def test_all_interior_blobs_are_captured(self):
        """The wand returned one blob; box-select must return both."""
        mask = self.build()
        selected, kept, rejected = core.select_in_box(
            mask, self.WIDTH, self.HEIGHT, box=(2, 2, 18, 18), min_area=4)

        areas = sorted(c.area for c in kept)
        self.assertEqual(areas, [9, 16])                  # blob B and blob A
        self.assertEqual(sum(selected), 25)               # 16 + 9

    def test_exterior_blob_is_excluded(self):
        mask = self.build()
        selected, kept, rejected = core.select_in_box(
            mask, self.WIDTH, self.HEIGHT, box=(2, 2, 18, 18), min_area=4)

        # Blob C sits at (30,30); nothing there may be selected.
        for y in range(30, 35):
            for x in range(30, 35):
                self.assertEqual(selected[y * self.WIDTH + x], 0)
        self.assertNotIn(25, [c.area for c in kept])

    def test_speck_below_min_area_is_rejected(self):
        mask = self.build()
        selected, kept, rejected = core.select_in_box(
            mask, self.WIDTH, self.HEIGHT, box=(2, 2, 18, 18), min_area=4)

        self.assertEqual([c.area for c in rejected], [1])
        self.assertEqual(selected[15 * self.WIDTH + 10], 0)

    def test_min_area_zero_keeps_the_speck(self):
        mask = self.build()
        selected, kept, rejected = core.select_in_box(
            mask, self.WIDTH, self.HEIGHT, box=(2, 2, 18, 18), min_area=0)
        self.assertEqual(rejected, [])
        self.assertEqual(sum(selected), 26)               # 16 + 9 + 1

    def test_box_straddling_a_blob_clips_before_filtering(self):
        """A blob half inside the box contributes only its interior half.

        This pins the documented ordering: intersect with the box first, then
        apply min_area. The alternative (filter on full blob area) would let a
        blob that is mostly outside drag a 1-pixel sliver into the selection.
        """
        pixels = make_frame(self.WIDTH, self.HEIGHT, background=10, blobs=[
            (8, 8, 8, 4, 200),      # 8 wide x 4 tall = 32 px, spanning x 8..15
        ])
        mask = core.mask_from_threshold(pixels, 100, 255)

        # Box covers x 0..11, so only x 8..11 of the blob is inside: 4 x 4 = 16.
        selected, kept, rejected = core.select_in_box(
            mask, self.WIDTH, self.HEIGHT, box=(0, 0, 12, 40), min_area=4)
        self.assertEqual([c.area for c in kept], [16])
        self.assertEqual(sum(selected), 16)

        # Now a box that catches only a 2-pixel sliver, below min_area=4:
        # the whole blob is rejected rather than partially selected.
        selected, kept, rejected = core.select_in_box(
            mask, self.WIDTH, self.HEIGHT, box=(14, 0, 2, 1), min_area=4)
        self.assertEqual(kept, [])
        self.assertEqual(sum(selected), 0)

    def test_diagonal_touch_respects_connectivity(self):
        pixels = make_frame(10, 10, background=0, blobs=[
            (2, 2, 1, 1, 200),
            (3, 3, 1, 1, 200),      # touches the first only diagonally
        ])
        mask = core.mask_from_threshold(pixels, 100, 255)

        _, kept8, _ = core.select_in_box(mask, 10, 10, (0, 0, 10, 10),
                                         min_area=1, connectivity=8)
        self.assertEqual(len(kept8), 1)          # one component of 2 px
        self.assertEqual(kept8[0].area, 2)

        _, kept4, _ = core.select_in_box(mask, 10, 10, (0, 0, 10, 10),
                                         min_area=1, connectivity=4)
        self.assertEqual(len(kept4), 2)          # two separate components

    def test_box_clamped_to_frame_bounds(self):
        mask = core.mask_from_threshold(
            make_frame(10, 10, 0, [(0, 0, 2, 2, 200)]), 100, 255)
        selected, kept, _ = core.select_in_box(
            mask, 10, 10, box=(-5, -5, 20, 20), min_area=1)
        self.assertEqual(sum(selected), 4)

    def test_box_entirely_outside_selects_nothing(self):
        mask = core.mask_from_threshold(
            make_frame(10, 10, 0, [(0, 0, 2, 2, 200)]), 100, 255)
        selected, kept, rejected = core.select_in_box(
            mask, 10, 10, box=(50, 50, 5, 5), min_area=1)
        self.assertEqual(sum(selected), 0)
        self.assertEqual(kept, [])


class TestSaturationAndFocus(unittest.TestCase):

    def test_saturated_fraction(self):
        pixels = [100] * 90 + [255] * 10
        self.assertAlmostEqual(core.saturated_fraction(pixels, 255), 0.10)

    def test_saturated_fraction_unknown_ceiling(self):
        self.assertIsNone(core.saturated_fraction([1, 2, 3], None))

    def test_saturation_value_for_bit_depth(self):
        self.assertEqual(core.saturation_value_for(8), 255)
        self.assertEqual(core.saturation_value_for(16), 65535)
        self.assertIsNone(core.saturation_value_for(32))

    def test_focus_score_ranks_sharp_above_blurred(self):
        # Sharp: a hard edge. Blurred: the same edge ramped over 3 pixels.
        sharp = make_frame(10, 10, 10, [(5, 0, 5, 10, 200)])
        blurred = make_frame(10, 10, 10, [(5, 0, 5, 10, 200)])
        for y in range(10):
            blurred[y * 10 + 4] = 73
            blurred[y * 10 + 5] = 136

        sharp_score = core.variance_of_laplacian(sharp, 10, 10)
        blurred_score = core.variance_of_laplacian(blurred, 10, 10)
        self.assertGreater(sharp_score, blurred_score)

    def test_focus_score_needs_a_usable_patch(self):
        self.assertIsNone(core.variance_of_laplacian([1, 2, 3, 4], 2, 2))


class TestRowBuilding(unittest.TestCase):

    CHANNELS = ["RFP", "GFP"]

    def eye(self):
        return core.stats_dict(area=50.0, mean=20.0, std_dev=1.0, median=20,
                               mode=20, minimum=18, maximum=22, perimeter=25.0,
                               pixel_count=50, raw_mean=20.0, skewness=0.0,
                               kurtosis=0.0)

    def signal(self):
        return core.stats_dict(area=100.0, mean=100.0, std_dev=5.0, median=100,
                               mode=100, minimum=90, maximum=110,
                               perimeter=40.0, pixel_count=100, raw_mean=100.0,
                               skewness=0.0, kurtosis=0.0)

    def background(self):
        return core.stats_dict(area=200.0, mean=10.0, std_dev=2.0, median=10,
                               mode=10, minimum=6, maximum=14, perimeter=60.0,
                               pixel_count=200, raw_mean=10.0, skewness=0.0,
                               kurtosis=0.0)

    def test_header_and_row_keys_agree_exactly(self):
        """Guards the drift that a shared key list is supposed to prevent."""
        header = core.csv_header(self.CHANNELS)
        row = core.build_row("img.tif", "Fish 1", self.CHANNELS,
                             eye_stats=self.eye(),
                             channel_results={
                                 "RFP": {"signal": self.signal(),
                                         "background": self.background()},
                                 "GFP": {"signal": self.signal(),
                                         "background": self.background()},
                             })
        self.assertEqual(sorted(header), sorted(row.keys()))
        self.assertEqual(len(header), len(set(header)))   # no duplicates

    def test_corrected_column_matches_the_formula(self):
        row = core.build_row("img.tif", "Fish 1", self.CHANNELS,
                             eye_stats=self.eye(),
                             channel_results={
                                 "GFP": {"signal": self.signal(),
                                         "background": self.background()},
                             })
        # IntDen 10000 - (10 * 100) = 9000
        self.assertEqual(row["GFP_Corrected"], 9000.0)
        self.assertEqual(row["GFP_Area"], 100.0)
        self.assertEqual(row["GFP_BGMean"], 10.0)
        self.assertEqual(row["EyeArea"], 50.0)

    def test_missing_background_leaves_bg_columns_blank(self):
        """No background measured is not the same claim as background zero."""
        row = core.build_row("img.tif", "Fish 1", ["GFP"],
                             channel_results={"GFP": {"signal": self.signal()}})
        self.assertEqual(row["GFP_BGMean"], "")
        self.assertEqual(row["GFP_Corrected"], 10000.0)   # nothing subtracted

    def test_skipped_is_distinguishable_from_missing(self):
        row = core.build_row("img.tif", "Fish 1", ["RFP", "GFP"],
                             eye_stats=core.SKIPPED,
                             channel_results={
                                 "RFP": {"signal": core.SKIPPED},
                                 # GFP absent entirely
                             })
        self.assertEqual(row["EyeArea"], core.SKIPPED)
        self.assertEqual(row["RFP_Area"], core.SKIPPED)
        self.assertEqual(row["RFP_Corrected"], core.SKIPPED)
        self.assertEqual(row["GFP_Area"], "")             # missing, not skipped

    def test_provenance_is_written_into_the_row(self):
        threshold = core.ThresholdResult(16.0, 255, core.THRESH_BACKGROUND,
                                         k=3.0, mean_bg=10.0, sd_bg=2.0)
        row = core.build_row("img.tif", "Fish 1", ["GFP"],
                             channel_results={"GFP": {
                                 "signal": self.signal(),
                                 "background": self.background(),
                                 "threshold": threshold,
                                 "provenance": {"BoxX": 10, "BoxY": 20,
                                                "BoxW": 30, "BoxH": 40,
                                                "MinArea": 4,
                                                "ComponentsKept": 2,
                                                "SaturatedFraction": 0.0},
                             }})
        self.assertEqual(row["GFP_ThresholdLow"], 16.0)
        self.assertEqual(row["GFP_ThresholdMethod"], core.THRESH_BACKGROUND)
        self.assertEqual(row["GFP_ThresholdK"], 3.0)
        self.assertEqual(row["GFP_ThresholdOverridden"], False)
        self.assertEqual(row["GFP_BoxW"], 30)
        self.assertEqual(row["GFP_ComponentsKept"], 2)

    def test_image_level_provenance(self):
        row = core.build_row("img.tif", "Fish 1", ["GFP"],
                             image_info={"PixelWidth": 0.325,
                                         "PixelUnit": "micron",
                                         "BitDepth": 16,
                                         "Calibrated": True,
                                         "PlaneOverride": False})
        self.assertEqual(row["PixelWidth"], 0.325)
        self.assertEqual(row["PixelUnit"], "micron")
        self.assertEqual(row["BitDepth"], 16)
        self.assertEqual(row["Operator"], "")   # not supplied, blank not absent


class TestRounding(unittest.TestCase):
    """Rounding must not depend on which Python is running the code."""

    def test_half_rounds_away_from_zero_not_to_even(self):
        # CPython 3's builtin round() would give 0.0, 2.0, 2.0 here (banker's
        # rounding). Jython 2.7's would give 1.0, 2.0, 3.0. We pin the Python 2
        # behaviour so Fiji and the test suite agree.
        self.assertEqual(core._round(0.5, 0), 1.0)
        self.assertEqual(core._round(1.5, 0), 2.0)
        self.assertEqual(core._round(2.5, 0), 3.0)
        self.assertEqual(core._round(-0.5, 0), -1.0)
        self.assertEqual(core._round(-2.5, 0), -3.0)

    def test_four_decimal_default(self):
        self.assertEqual(core._round(1.00005, 4), 1.0001)
        self.assertEqual(core._round(123.456789, 4), 123.4568)

    def test_non_finite_and_non_numeric_pass_through(self):
        self.assertEqual(core._round(None, 4), "")
        self.assertEqual(core._round(float("nan"), 4), "")
        self.assertEqual(core._round("SKIPPED", 4), "SKIPPED")
        self.assertEqual(core._round(True, 4), True)


class TestPortability(unittest.TestCase):
    """core.py must stay importable under Jython 2.7 as well as CPython 3."""

    def test_core_has_no_imagej_or_numpy_imports(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "zfquant", "core.py")
        with open(path) as handle:
            source = handle.read()
        for banned in ("import ij", "from ij", "import numpy", "from numpy",
                       "import java", "from java", "import javax"):
            self.assertNotIn(banned, source,
                             "core.py must not depend on %r" % banned)

    def test_core_uses_no_python3_only_syntax(self):
        """f-strings and annotations would break under Jython 2.7."""
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "zfquant", "core.py")
        with open(path) as handle:
            lines = handle.readlines()
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            self.assertFalse(
                stripped.startswith('f"') or stripped.startswith("f'") or
                ' f"' in line or " f'" in line,
                "f-string at core.py:%d is not valid under Jython 2.7" % number)


if __name__ == "__main__":
    unittest.main()
