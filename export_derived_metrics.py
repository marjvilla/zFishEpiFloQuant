#!/usr/bin/env python3
"""Export size- and intensity-normalized per-channel metrics from a
zfquant session's dataset CSV.

Run after measuring in Fiji:

    python3 export_derived_metrics.py path/to/<session>_dataset.csv

Writes <session>_dataset_derived.csv alongside the input by default, or
pass a second argument to choose the output path. See zfquant/derive.py
for what each column means and why.
"""
import sys

from zfquant.derive import export_derived_csv


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 export_derived_metrics.py <dataset.csv> [output.csv]")
        sys.exit(1)
    input_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None
    written, channels = export_derived_csv(input_path, output_path)
    print("Wrote %s (channels: %s)" % (written, ", ".join(channels)))


if __name__ == "__main__":
    main()
