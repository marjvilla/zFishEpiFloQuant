# Reading and analyzing Zebrafish Quant output

Reference for anyone (human or agent) analyzing a session's CSV. Everything
here is the schema as the code actually writes it — see `zfquant/core.py`
(`csv_header`, `STAT_KEYS`, `PROVENANCE_KEYS`, `IMAGE_KEYS`) and
`zfquant/derive.py` if you need to confirm anything.

## What the tool measures

Per fish, per fluorescent channel, the operator draws two regions on a single
plane:

- a **background box** in a fish-free area (ambient/lamp fluorescence)
- a **signal box** around the organ of interest; within that box, pixels above
  a threshold the operator sets are the measured region

Separately, one **eye ROI** is drawn on brightfield. Eye area is the size proxy
used to normalize across fish of different sizes.

```
Corrected Intensity = FL IntDen - (Mean BG x FL Area)
                    = FL Area x (FL Mean - BG Mean)
```

So `{channel}_Corrected` is already "size x background-corrected intensity."

## Files in a session folder

`<output folder>/<session name>/`

| File | What it is |
|---|---|
| `<session>_dataset.csv` | **The data.** One row per fish. |
| `<session>_dataset_derived.csv` | Generated. Raw + normalized columns. |
| `<session>_dataset_summary.csv` | Generated. Stats-ready subset. |
| `<session>_journal.jsonl` | Append-only log. Source of truth. |
| `<session>_manifest.json` | Which image plane each fish/channel came from. |
| `ROIs/<session>_all_ROIs.zip` | Every ROI drawn, ImageJ format. |
| `Audit_Images/<session>_Fish{N}_{channel}.png` | Per-channel visual check. Brightfield is `_Eye`, not the channel name. |

The two generated CSVs may not exist yet. Create them with:

```bash
python3 export_derived_metrics.py "<path>/<session>_dataset.csv"
```

Never edit `_journal.jsonl` — the dataset CSV is rebuilt from it. Undone fish
are already excluded from the CSV (tombstoned in the journal), so the CSV is
the correct row set; don't try to reconstruct rows from the journal yourself.

## `_dataset.csv` layout

102 columns for a 2-fluorescent-channel session. One row per fish. Structure:

```
FileName, FishID
<10 image/provenance columns>
Eye<stat> x12
for each fluorescent channel:
    {ch}_<stat>   x12    signal region
    {ch}_BG<stat> x12    background region
    {ch}_Corrected
    {ch}_<provenance> x14
```

### Identity

| Column | Notes |
|---|---|
| `FileName` | Source image title. |
| `FishID` | `"Fish 1"`, `"Fish 2"`, … Sequential within the session. |

⚠️ **There is no condition, genotype, or treatment column.** Nothing in the
schema says which fish is control vs. experimental. That mapping has to come
from the user, or be parsed from `FileName` if it encodes it. Never invent
groupings — ask.

### Image / provenance (10 columns)

| Column | Notes |
|---|---|
| `PixelWidth`, `PixelUnit` | Spatial calibration, e.g. `2.439`, `micron`. |
| `BitDepth` | 8/16-bit. Sets the saturation ceiling. |
| `Calibrated` | `TRUE`/`FALSE`. **See units warning below.** |
| `FocusScore` | Higher = sharper. Often blank/`NaN`. Relative, not absolute. |
| `PlaneOverride` | `TRUE` if the operator redirected this fish's plane. |
| `PlaneRecorded` | Plane actually measured, e.g. `c1 t2`, `slice 47`. |
| `PlaneExpected` | What the plan predicted, when it differed. |
| `Operator`, `MeasuredAt` | Who/when. `MeasuredAt` is ISO-ish local time. |

### The 12 stats (`STAT_KEYS`)

Applied with prefix `Eye`, `{ch}_`, and `{ch}_BG`:

`Area`, `Mean`, `StdDev`, `Median`, `Mode`, `Min`, `Max`, `Perimeter`,
`IntDen`, `RawIntDen`, `Skewness`, `Kurtosis`

- `IntDen` = calibrated area × calibrated mean (real units)
- `RawIntDen` = pixel count × uncalibrated mean (pixel units)
- Use **`IntDen`**, not `RawIntDen`, unless you specifically want pixel units.
- The eye has no `BG` block and no `Corrected` — it's a size measurement.

### Per-channel provenance (14 columns)

| Column | Notes |
|---|---|
| `ThresholdLow` / `ThresholdHigh` | The actual cutoff used. |
| `ThresholdMethod` | `background_sd`, `percentile`, or `manual`. |
| `ThresholdK` | k in `mean_bg + k*sd_bg`. Blank when manual. |
| `ThresholdOverridden` | `TRUE` when the operator set it by hand. |
| `MinArea` | Speck-rejection size, default 4 px. |
| `BoxX/Y/W/H` | Signal box geometry, pixels. |
| `SignalPixels` | Pixels above threshold that were counted. |
| `ComponentsKept` / `ComponentsRejected` | Blobs kept vs. dropped as specks. |
| `SaturatedFraction` | 0–1. Fraction of signal pixels at the ceiling. |

**`ThresholdOverridden=TRUE` and `ThresholdMethod=manual` are normal**, not a
defect — the workflow is designed around the operator setting the threshold.
A `ThresholdLow` of `0.0` combined with a tight signal box is also normal and
does not invalidate the row; the box does the spatial selection.

## Blank vs. `SKIPPED`

Two different meanings — do not conflate:

| Value | Meaning | Handling |
|---|---|---|
| `SKIPPED` | Deliberately omitted; this fish has no such channel. | Legitimate. Exclude from that channel's stats; keep the fish for others. |
| *(empty)* | Not measured / measurement failed. | Investigate. Usually also excluded, but it's unplanned. |

Both are non-numeric — coerce with something that yields NA rather than 0.
**Treating either as zero silently fabricates data.**

## The generated CSVs

`derive.py` computes these; nothing is re-measured.

| Column | Formula | Isolates |
|---|---|---|
| `{ch}_AreaFrac_Eye` | `{ch}_Area / EyeArea` | **Size only** |
| `{ch}_Mean` | passthrough | **Intensity only** |
| `{ch}_CorrectedPerEyeArea` | `{ch}_Corrected / EyeArea` | **Size × intensity** |

`_summary.csv` is `FishID` + those three per channel, one row per fish — the
stats-ready one.
`_derived.csv` additionally keeps `FileName`, `{ch}_Area`, `{ch}_Corrected`.

**Which to use:** `CorrectedPerEyeArea` is the headline metric for "how much
signal, adjusted for fish size." Report `AreaFrac_Eye` and `Mean` alongside it
— if the headline number moves, those two say whether it was driven by a
larger region, brighter signal, or both. That decomposition is usually the
biologically interesting part.

## Quick analysis recipe

1. **Generate the derived CSVs** (command above) if absent.
2. **Sanity-check the schema** — channel count, row count, that `FishID` is
   unique. Row count should equal the number of fish measured.
3. **QC pass.** Flag, report, and only then decide with the user whether to
   exclude:
   - `SaturatedFraction` > ~0.01 → intensity is clipped; `Mean`/`IntDen`
     understate the truth by an unknown amount. Area is still usable.
   - `Calibrated = FALSE` → see units warning.
   - Mixed `PixelWidth` / `PixelUnit` across rows → areas are not comparable.
   - `PlaneOverride = TRUE` → fine, but worth noting.
   - Extreme `{ch}_Area` or `EyeArea` outliers → often a mis-drawn ROI; check
     that fish's audit image before dropping it.
4. **Get the grouping from the user.** There is no condition column.
5. **Analyze** the three normalized metrics per channel. With two groups and
   roughly normal data, an unpaired t-test on `CorrectedPerEyeArea` is the
   usual test; report n, mean ± SD, and the test statistic. Prefer a
   non-parametric test for small or skewed groups. Say which you used.
6. **Report all three metrics**, not just the significant one.

## Gotchas

- **Units.** When `Calibrated=TRUE`, `Area` is in `PixelUnit²` (e.g. micron²)
  and `IntDen` is calibrated-area × mean. When `FALSE`, areas are px². Mixing
  calibrated and uncalibrated rows in one comparison is invalid — check
  `Calibrated` and `PixelWidth` are consistent before pooling.
- **Ratios are already normalized.** Don't divide `CorrectedPerEyeArea` by eye
  area again.
- **Verify channel labels look right** before drawing conclusions. If one
  channel's values look implausible relative to the other, mis-assigned
  channels are a real failure mode; the audit images in `Audit_Images/` show
  what was actually measured for each fish and channel.
- **Rows aren't independent across channels.** `RFP_*` and `GFP_*` on one row
  are the same animal — paired, not independent samples.
- **`FishID` is per-session.** "Fish 1" in two sessions are different animals.
  Namespace by session before pooling.
- **Small n.** These sessions are often 6–20 fish. Report effect sizes and
  actual n; don't lean on p-values alone.
