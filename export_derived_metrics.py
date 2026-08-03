#!/usr/bin/env python3
"""Export size- and intensity-normalized per-channel metrics from a
zfquant session's dataset CSV.

Run after measuring in Fiji (this is a standalone step -- it never runs
inside Fiji itself, only on the CSV the plugin already wrote):

    python3 export_derived_metrics.py path/to/<session>_dataset.csv

Writes two files alongside the input by default:
  <session>_dataset_derived.csv   every raw + normalized column
  <session>_dataset_summary.csv   just FishID + the normalized metrics,
                                   one row per fish -- for pasting into a
                                   t-test / stats tool

Pass a second argument to choose where the derived (full) file goes; the
summary file is always written next to it with "_summary" in place of
"_derived". See zfquant/derive.py for what each column means and why.
"""
import sys

from zfquant.derive import export_derived_csv, export_summary_csv


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 export_derived_metrics.py <dataset.csv> [output.csv]")
        sys.exit(1)
    input_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None

    derived_path, channels = export_derived_csv(input_path, output_path)
    summary_output = (derived_path.replace("_derived", "_summary")
                      if output_path else None)
    summary_path, _ = export_summary_csv(input_path, summary_output)

    print("Wrote %s (channels: %s)" % (derived_path, ", ".join(channels)))
    print("Wrote %s" % summary_path)


if __name__ == "__main__":
    main()
