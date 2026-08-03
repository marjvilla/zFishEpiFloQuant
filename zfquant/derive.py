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

Two exports: export_derived_csv (every raw + normalized column, for
checking the normalization itself) and export_summary_csv (just FishID and
the three normalized metrics per channel, meant to be pasted straight into
a t-test / stats tool without stripping columns first).
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


def summary_header(channel_names):
    """Just the three normalized/comparable metrics per channel -- no
    FileName, no raw passthroughs (Area, Corrected) -- for pasting straight
    into a t-test / stats tool without extra columns to strip first."""
    header = ["FishID"]
    for name in channel_names:
        header += ["%s_AreaFrac_Eye" % name, "%s_Mean" % name,
                   "%s_CorrectedPerEyeArea" % name]
    return header


def summary_row(row, channel_names, round_to=4):
    full = derive_row(row, channel_names, round_to)
    out = {"FishID": full["FishID"]}
    for name in channel_names:
        for suffix in ("AreaFrac_Eye", "Mean", "CorrectedPerEyeArea"):
            key = "%s_%s" % (name, suffix)
            out[key] = full[key]
    return out


def _read_dataset_csv(input_path):
    with open(input_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        rows = list(reader)
    return header, rows


def _default_output_path(input_path, suffix):
    base, ext = os.path.splitext(input_path)
    return base + suffix + (ext or ".csv")


def export_derived_csv(input_path, output_path=None, round_to=4):
    """Read a zfquant session dataset CSV and write a derived-metrics CSV
    next to it (or at `output_path`) -- every raw and normalized column, for
    checking the normalization itself. Returns (output_path, channel_names).
    See export_summary_csv for a stats-ready, columns-only-you-need version.
    """
    header, rows = _read_dataset_csv(input_path)
    channel_names = fluorescence_channel_names(header)
    out_header = derived_header(channel_names)
    derived_rows = [derive_row(row, channel_names, round_to) for row in rows]

    if output_path is None:
        output_path = _default_output_path(input_path, "_derived")

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_header, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(derived_rows)

    return output_path, channel_names


def export_summary_csv(input_path, output_path=None, round_to=4):
    """Same source data as export_derived_csv, but only FishID and the
    three normalized metrics per channel -- meant to be pasted straight
    into a t-test / stats tool, one row per fish."""
    header, rows = _read_dataset_csv(input_path)
    channel_names = fluorescence_channel_names(header)
    out_header = summary_header(channel_names)
    summary_rows = [summary_row(row, channel_names, round_to) for row in rows]

    if output_path is None:
        output_path = _default_output_path(input_path, "_summary")

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_header, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(summary_rows)

    return output_path, channel_names
