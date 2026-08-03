# -*- coding: utf-8 -*-
"""Tests for zfquant.derive -- post-hoc size/intensity-normalized metrics.

Run with:  python3 -m unittest discover -s tests -v
"""

import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zfquant import derive   # noqa: E402


class TestFluorescenceChannelNames(unittest.TestCase):

    def test_detects_channels_from_area_columns(self):
        header = ["FileName", "FishID", "EyeArea", "EyeMean",
                  "RFP_Area", "RFP_Mean", "GFP_Area", "GFP_Mean"]
        self.assertEqual(derive.fluorescence_channel_names(header),
                         ["RFP", "GFP"])

    def test_eye_area_is_not_a_channel(self):
        header = ["EyeArea", "RFP_Area"]
        self.assertEqual(derive.fluorescence_channel_names(header), ["RFP"])

    def test_no_channels_found_is_empty_list(self):
        self.assertEqual(derive.fluorescence_channel_names(["EyeArea"]), [])


class TestDeriveRow(unittest.TestCase):

    def test_area_frac_and_corrected_per_eye_area(self):
        row = {"FileName": "f.tif", "FishID": "Fish 1", "EyeArea": "1000",
              "RFP_Area": "250", "RFP_Mean": "500", "RFP_Corrected": "8000"}
        out = derive.derive_row(row, ["RFP"])
        self.assertEqual(out["RFP_Area"], 250)
        self.assertEqual(out["RFP_AreaFrac_Eye"], 0.25)
        self.assertEqual(out["RFP_Mean"], 500)
        self.assertEqual(out["RFP_Corrected"], 8000)
        self.assertEqual(out["RFP_CorrectedPerEyeArea"], 8)

    def test_skipped_channel_is_blank_not_an_error(self):
        row = {"FileName": "f.tif", "FishID": "Fish 1", "EyeArea": "1000",
              "RFP_Area": "SKIPPED", "RFP_Mean": "SKIPPED",
              "RFP_Corrected": "SKIPPED"}
        out = derive.derive_row(row, ["RFP"])
        self.assertEqual(out["RFP_Area"], "")
        self.assertEqual(out["RFP_AreaFrac_Eye"], "")
        self.assertEqual(out["RFP_CorrectedPerEyeArea"], "")

    def test_missing_eye_area_does_not_raise(self):
        row = {"FileName": "f.tif", "FishID": "Fish 1", "EyeArea": "",
              "RFP_Area": "250", "RFP_Corrected": "8000"}
        out = derive.derive_row(row, ["RFP"])
        self.assertEqual(out["RFP_Area"], 250)
        self.assertEqual(out["RFP_AreaFrac_Eye"], "")
        self.assertEqual(out["RFP_CorrectedPerEyeArea"], "")

    def test_zero_eye_area_does_not_divide_by_zero(self):
        row = {"FileName": "f.tif", "FishID": "Fish 1", "EyeArea": "0",
              "RFP_Area": "250", "RFP_Corrected": "8000"}
        out = derive.derive_row(row, ["RFP"])
        self.assertEqual(out["RFP_AreaFrac_Eye"], "")
        self.assertEqual(out["RFP_CorrectedPerEyeArea"], "")

    def test_rounds_to_requested_precision(self):
        row = {"EyeArea": "3", "RFP_Area": "1"}
        out = derive.derive_row(row, ["RFP"], round_to=2)
        self.assertEqual(out["RFP_AreaFrac_Eye"], 0.33)


class TestExportDerivedCsv(unittest.TestCase):

    def test_round_trip_through_a_real_file(self):
        tmpdir = tempfile.mkdtemp()
        try:
            input_path = os.path.join(tmpdir, "dataset.csv")
            with open(input_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f, lineterminator="\r\n")
                writer.writerow(["FileName", "FishID", "EyeArea",
                                 "RFP_Area", "RFP_Mean", "RFP_Corrected",
                                 "GFP_Area", "GFP_Mean", "GFP_Corrected"])
                writer.writerow(["f.tif", "Fish 1", "1000",
                                 "250", "500", "8000",
                                 "100", "300", "3000"])

            output_path, channels = derive.export_derived_csv(input_path)
            self.assertEqual(channels, ["RFP", "GFP"])
            self.assertTrue(output_path.endswith("dataset_derived.csv"))

            with open(output_path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["RFP_AreaFrac_Eye"], "0.25")
            self.assertEqual(rows[0]["GFP_AreaFrac_Eye"], "0.1")
        finally:
            import shutil
            shutil.rmtree(tmpdir)


class TestSummaryRow(unittest.TestCase):

    def test_only_normalized_columns_no_raw_passthroughs(self):
        row = {"FileName": "f.tif", "FishID": "Fish 1", "EyeArea": "1000",
              "RFP_Area": "250", "RFP_Mean": "500", "RFP_Corrected": "8000"}
        out = derive.summary_row(row, ["RFP"])
        self.assertEqual(sorted(out.keys()),
                         ["FishID", "RFP_AreaFrac_Eye", "RFP_CorrectedPerEyeArea",
                          "RFP_Mean"])
        self.assertNotIn("FileName", out)
        self.assertEqual(out["RFP_AreaFrac_Eye"], 0.25)
        self.assertEqual(out["RFP_CorrectedPerEyeArea"], 8)


class TestExportSummaryCsv(unittest.TestCase):

    def test_round_trip_through_a_real_file(self):
        tmpdir = tempfile.mkdtemp()
        try:
            input_path = os.path.join(tmpdir, "dataset.csv")
            with open(input_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f, lineterminator="\r\n")
                writer.writerow(["FileName", "FishID", "EyeArea",
                                 "RFP_Area", "RFP_Mean", "RFP_Corrected"])
                writer.writerow(["f.tif", "Fish 1", "1000", "250", "500", "8000"])

            output_path, channels = derive.export_summary_csv(input_path)
            self.assertEqual(channels, ["RFP"])
            self.assertTrue(output_path.endswith("dataset_summary.csv"))

            with open(output_path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["FishID"], "Fish 1")
            self.assertEqual(rows[0]["RFP_AreaFrac_Eye"], "0.25")
            self.assertNotIn("RFP_Area", rows[0])
        finally:
            import shutil
            shutil.rmtree(tmpdir)


if __name__ == "__main__":
    unittest.main()
