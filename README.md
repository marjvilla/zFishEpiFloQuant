# Zebrafish Quant

A Fiji/ImageJ tool for quantifying fluorescent signal (GFP, RFP, ...) in
zebrafish epi-fluorescence images, normalized against eye area from
brightfield.

Repo: https://github.com/marjvilla/zFishEpiFloQuant

```
Corrected Intensity = FL Integrated Density - (Mean Background Intensity x FL Area)
```

## Install (one time, per computer)

Requires [Fiji](https://fiji.sc) already installed, and `git`.

```bash
git clone https://github.com/marjvilla/zFishEpiFloQuant.git
cd zFishEpiFloQuant
./install.sh
```

Then fully quit and reopen Fiji. The tool appears as a normal menu item:

**Plugins > ZebrafishQuant > Zebrafish Quant**

No need to open the Script Editor, pick a language, or navigate to a file --
click the menu item like any other Fiji plugin.

If `install.sh` can't find your Fiji install automatically, point it at the
folder directly:

```bash
./install.sh /Applications/Fiji        # or wherever your Fiji folder is
```

### Manual install (Windows, or if the script doesn't work for you)

Symlink (not copy) two things from this repo, to two DIFFERENT places --
this split matters, see the note below:

- `Zebrafish_Quant.py` into `<your Fiji folder>/scripts/Plugins/ZebrafishQuant/`
- the `zfquant/` folder into `<your Fiji folder>/jars/Lib/`

```
<your Fiji folder>/
  scripts/Plugins/ZebrafishQuant/Zebrafish_Quant.py  -> repo/Zebrafish_Quant.py
  jars/Lib/zfquant                                    -> repo/zfquant
```

- **macOS/Linux**, by hand:
  ```bash
  mkdir -p "/path/to/Fiji/scripts/Plugins/ZebrafishQuant" "/path/to/Fiji/jars/Lib"
  ln -s "$(pwd)/Zebrafish_Quant.py" "/path/to/Fiji/scripts/Plugins/ZebrafishQuant/Zebrafish_Quant.py"
  ln -s "$(pwd)/zfquant" "/path/to/Fiji/jars/Lib/zfquant"
  ```
- **Windows** (Command Prompt as Administrator, or with Developer Mode on):
  ```
  mkdir "C:\Fiji.app\scripts\Plugins\ZebrafishQuant"
  mkdir "C:\Fiji.app\jars\Lib"
  mklink "C:\Fiji.app\scripts\Plugins\ZebrafishQuant\Zebrafish_Quant.py" "C:\path\to\repo\Zebrafish_Quant.py"
  mklink /D "C:\Fiji.app\jars\Lib\zfquant" "C:\path\to\repo\zfquant"
  ```

Symlinks matter here, not copies: it's what makes updating later a single
`git pull` with nothing else to redo.

**Why two locations, not one:** Fiji's script menu recurses into every
folder under `scripts/Plugins` and turns each `.py` file it finds into its
own clickable menu entry. If `zfquant/` sat next to `Zebrafish_Quant.py`,
every internal module inside it (`core.py`, `fiji_io.py`, ...) would show up
as its own bogus menu item under a "zfquant" submenu. `jars/Lib` is on
Jython's import path but is never scanned for menu items, so only the real
entry point appears in the menu.

## Updating

```bash
git pull
```

That's it. The Fiji menu item is a symlink into this folder, so whatever is
checked out here is what runs -- no re-copying, no re-running `install.sh`.
Just quit and reopen Fiji (or Help > Refresh Menus) to pick up the change.

## Using it

Open your images in Fiji first, then run the tool from the Plugins menu.
Setup walks you through:

1. Session name, channel names, and one of three modes:
   - **Auto Hyperstack** -- one image with a real channel dimension
   - **Auto Single Stack** -- one stack holding every fish and channel as slices
   - **Manual** -- you tell it what each open image is
2. An interactive review where you step through the real data (not blind
   text fields) confirming or correcting the plan before anything is measured.
3. Then the normal per-fish workflow: arm a channel, draw a background box,
   draw a signal box, accept, repeat, save the fish.

Output goes to `<output folder>/<session name>/`: a CSV, an ROI archive, and
per-channel audit images, all written incrementally as you go (not batched to
the end), so a crash mid-session loses at most the one fish in progress.

## Size + intensity metrics (after measuring)

A standalone side tool, separate from the Fiji plugin -- it never runs
inside Fiji, only afterward, on your computer, against the CSV the plugin
already wrote. The dataset CSV has raw stats per channel, but comparing fish
of different sizes needs those normalized. Once a session's CSV is done:

```bash
python3 export_derived_metrics.py "<output folder>/<session name>/<session name>_dataset.csv"
```

Writes two files next to it:

- `..._dataset_derived.csv` -- every raw + normalized column, per
  fluorescence channel:
  - `{channel}_AreaFrac_Eye` -- fluorescent area / eye area (size only)
  - `{channel}_Mean` -- intensity density (amount only, no area)
  - `{channel}_CorrectedPerEyeArea` -- background-subtracted integrated
    density (already area x intensity) / eye area -- the combined
    size-and-amount metric, comparable across fish of different sizes.
- `..._dataset_summary.csv` -- just `FishID` and the three normalized
  metrics above per channel (no `FileName`, no raw `Area`/`Corrected`),
  one row per fish -- meant to be pasted straight into a t-test / stats
  tool without stripping columns first.

See `zfquant/derive.py` for the reasoning behind these.

## Repo layout

```
Zebrafish_Quant.py       thin Fiji entry point
export_derived_metrics.py  CLI: size/intensity-normalized CSV (run on your
                            computer, after measuring -- see above)
zfquant/
  core.py             measurement + threshold math      (pure, unit-tested)
  manifest.py         fish -> plane mapping + overrides  (pure, unit-tested)
  journal.py          append-only session state          (pure, unit-tested)
  derive.py           post-hoc size/intensity metrics    (pure, unit-tested)
  fiji_io.py           every ImageJ/Java call lives here
  workflow.py          the per-fish state machine
  review.py             the interactive setup-time review panel
  ui.py                 the measurement-time control panel
  startup.py            setup dialogs (before review)
tests/                pytest/unittest suite for the pure modules above
legacy/               the original single-file script this replaced, kept
                       for reference
overview.md           design notes from the original build
```

## Development

The pure modules (`core.py`, `manifest.py`, `journal.py`) run under plain
Python 3, no Fiji required:

```bash
python3 -m unittest discover -s tests -v
```

Everything else (`fiji_io.py`, `workflow.py`, `ui.py`, `startup.py`,
`review.py`) is Jython-only and can only be exercised by actually running it
in Fiji -- there is no way to unit test it outside that environment.
