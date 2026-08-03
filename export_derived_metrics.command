#!/bin/bash
# Drag a session's "..._dataset.csv" onto this file's icon (or double-click
# it and paste the path when asked) to export the derived + summary CSVs --
# no Terminal typing required.
cd "$(dirname "$0")"

if [ -n "$1" ]; then
    CSV_PATH="$1"
else
    echo "Drag a dataset CSV onto this file's icon next time to skip this."
    echo "Paste the path to a ..._dataset.csv file and press Enter:"
    read -r CSV_PATH
fi

python3 export_derived_metrics.py "$CSV_PATH"

echo
read -r -p "Press Enter to close this window..."
