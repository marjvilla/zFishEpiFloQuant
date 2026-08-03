"""Post-hoc derived metrics: size + amount of fluorescence, normalized.

Python 3 only -- this runs separately from Fiji, on the tool's own output
CSV (``<output>/<session>/<session>_dataset.csv``), after measurement is
done. Not imported by anything Jython-side.

Fluorescence area alone doesn't say much across fish of different sizes,
and integrated density alone conflates "brighter" with "bigger fish" --
integrated density is already mean intensity x area, so it's already a
combined size+amount quantity in absolute terms. Every metric here
normalizes by EyeArea (the tool's existing size/growth-stage proxy) so
fish of different sizes are comparable:

  {channel}_AreaFrac_Eye         FL area / eye area -- how much of the
                                  eye's footprint is signal, size-only.
  {channel}_Mean                 passthrough: intensity density alone, no
                                  area at all -- kept separate so it's
                                  possible to tell which of area or
                                  intensity is driving a difference.
  {channel}_CorrectedPerEyeArea  background-subtracted integrated density
                                  (already amount x extent) / eye area --
                                  the recommended combined size+intensity
                                  metric.
"""

import csv
import os


def fluorescence_channel_names(header):
    """Auto-detect channel names from a zfquant CSV header: any column
    ending in "_Area" names a fluorescence channel. The eye's own Area
    column is spelled "EyeArea" (no underscore), so it's never matched."""
    names = []
    for column in header:
        if column.endswith("_Area") and not column.startswith("Eye"):
            names.append(column[:-len("_Area")])
    return names


def _to_float(value):
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text == "SKIPPED":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _fmt(value, round_to):
    if value is None:
        return ""
    return round(value, round_to)


def derived_header(channel_names):
    header = ["FileName", "FishID"]
    for name in channel_names:
        header += ["%s_Area" % name, "%s_AreaFrac_Eye" % name,
                   "%s_Mean" % name, "%s_Corrected" % name,
                   "%s_CorrectedPerEyeArea" % name]
    return header


def derive_row(row, channel_names, round_to=4):
    """One derived-metrics row for one fish's source CSV row (a dict, e.g.
    from csv.DictReader). Blank wherever the source is missing or the
    channel was deliberately omitted (SKIPPED) for this fish."""
    eye_area = _to_float(row.get("EyeArea"))
    out = {"FileName": row.get("FileName", ""), "FishID": row.get("FishID", "")}
    for name in channel_names:
        area = _to_float(row.get("%s_Area" % name))
        mean = _to_float(row.get("%s_Mean" % name))
        corrected = _to_float(row.get("%s_Corrected" % name))

        area_frac = area / eye_area if area is not None and eye_area else None
        corrected_per_eye = (corrected / eye_area
                             if corrected is not None and eye_area else None)

        out["%s_Area" % name] = _fmt(area, round_to)
        out["%s_AreaFrac_Eye" % name] = _fmt(area_frac, round_to)
        out["%s_Mean" % name] = _fmt(mean, round_to)
        out["%s_Corrected" % name] = _fmt(corrected, round_to)
        out["%s_CorrectedPerEyeArea" % name] = _fmt(corrected_per_eye, round_to)
    return out


def export_derived_csv(input_path, output_path=None, round_to=4):
    """Read a zfquant session dataset CSV and write a derived-metrics CSV
    next to it (or at `output_path`). Returns (output_path, channel_names)."""
    with open(input_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        rows = list(reader)

    channel_names = fluorescence_channel_names(header)
    out_header = derived_header(channel_names)
    derived_rows = [derive_row(row, channel_names, round_to) for row in rows]

    if output_path is None:
        base, ext = os.path.splitext(input_path)
        output_path = base + "_derived" + (ext or ".csv")

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_header, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(derived_rows)

    return output_path, channel_names
