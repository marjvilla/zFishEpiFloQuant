"""Post-hoc derived metrics: size + amount of fluorescence, normalized.

Jython 2.7 and CPython 3 compatible -- unlike the earlier version of this
module, it now runs both as the standalone CLI (export_derived_metrics.py,
on your computer, after measurement) AND from inside Fiji itself (the
"Export summary CSVs" button, see ui.py), on the tool's own output CSV
(``<output>/<session>/<session>_dataset.csv``). No ``ij.*`` imports.

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

CSV reading/writing is hand-rolled rather than the stdlib csv module, for
the same reason journal.py's _csv_line/_csv_field are -- csv wants binary
mode under Jython 2.7 and text mode under CPython 3, and this module has to
behave identically in both. Rounding is hand-rolled too (see _round) since
Python 2's round() rounds half away from zero and Python 3's rounds half to
even -- pinning the rule here keeps a summary CSV built inside Fiji
byte-identical to one built by the standalone CLI on the same data.
"""

import math
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
    text = value.strip() if isinstance(value, str) else str(value).strip()
    if text == "" or text == "SKIPPED":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _round(value, digits):
    """Round half away from zero, identically under Jython 2.7 and CPython
    3. See core._round -- the same trap, fixed the same way, kept as a
    separate copy so this module has no dependency on core.py."""
    try:
        if math.isnan(value):
            return None
        if math.isinf(value):
            return value
        factor = 10.0 ** digits
        scaled = value * factor
        if scaled >= 0:
            return math.floor(scaled + 0.5) / factor
        return math.ceil(scaled - 0.5) / factor
    except (TypeError, ValueError, OverflowError):
        return value


def _fmt(value, round_to):
    if value is None:
        return ""
    rounded = _round(value, round_to)
    return "" if rounded is None else rounded


def derived_header(channel_names):
    header = ["FileName", "FishID"]
    for name in channel_names:
        header += ["%s_Area" % name, "%s_AreaFrac_Eye" % name,
                   "%s_Mean" % name, "%s_Corrected" % name,
                   "%s_CorrectedPerEyeArea" % name]
    return header


def derive_row(row, channel_names, round_to=4):
    """One derived-metrics row for one fish's source CSV row (a dict of
    column name -> string value). Blank wherever the source is missing or
    the channel was deliberately omitted (SKIPPED) for this fish."""
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


# ---------------------------------------------------------------------------
#  CSV I/O -- hand-rolled, see the module docstring for why
# ---------------------------------------------------------------------------

def _csv_field(value):
    """Minimal RFC 4180 quoting -- matches journal.py's _csv_field."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if any(ch in text for ch in (',', '"', '\n', '\r')):
        return '"' + text.replace('"', '""') + '"'
    return text


def _csv_line(values):
    return ",".join(_csv_field(v) for v in values) + "\r\n"


def _parse_csv(text):
    """Minimal RFC 4180 parser matching _csv_field's quoting exactly, so
    this module never depends on the stdlib csv module's differing binary-
    vs-text-mode behavior between Jython 2.7 and CPython 3."""
    rows = []
    row = []
    field_chars = []
    in_quotes = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_quotes:
            if ch == '"':
                if i + 1 < n and text[i + 1] == '"':
                    field_chars.append('"')
                    i += 2
                    continue
                in_quotes = False
                i += 1
                continue
            field_chars.append(ch)
            i += 1
            continue
        if ch == '"':
            in_quotes = True
            i += 1
            continue
        if ch == ',':
            row.append("".join(field_chars))
            field_chars = []
            i += 1
            continue
        if ch == '\r':
            i += 1
            continue
        if ch == '\n':
            row.append("".join(field_chars))
            field_chars = []
            rows.append(row)
            row = []
            i += 1
            continue
        field_chars.append(ch)
        i += 1
    if field_chars or row:
        row.append("".join(field_chars))
        rows.append(row)
    return rows


def _read_dataset_csv(input_path):
    handle = open(input_path, "rb")
    try:
        raw = handle.read()
    finally:
        handle.close()
    text = raw.decode("utf-8")
    parsed = _parse_csv(text)
    if not parsed:
        return [], []
    header = parsed[0]
    rows = [dict(zip(header, values)) for values in parsed[1:]]
    return header, rows


def _write_csv(output_path, header, rows):
    lines = [_csv_line(header)]
    for row in rows:
        lines.append(_csv_line([row.get(column, "") for column in header]))
    payload = "".join(lines).encode("utf-8", "replace")

    handle = open(output_path, "wb")
    try:
        handle.write(payload)
    finally:
        handle.close()


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

    _write_csv(output_path, out_header, derived_rows)
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

    _write_csv(output_path, out_header, summary_rows)
    return output_path, channel_names
