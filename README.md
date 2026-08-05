# Zebrafish Quant

A Fiji/ImageJ tool for quantifying fluorescent signal (GFP, RFP, ...) in
zebrafish epi-fluorescence images, normalized against eye area from
brightfield.

Repo: https://github.com/marjvilla/zFishEpiFloQuant

```
Corrected Intensity = FL Integrated Density - (Mean Background Intensity x FL Area)
```

## Install (one time, per computer)

Requires [Fiji](https://fiji.sc) already installed.

1. Download the code: either `git clone` it (recommended -- see below), or
   grab the [zip from GitHub](https://github.com/marjvilla/zFishEpiFloQuant)
   (Code > Download ZIP) and unzip it.
2. **Double-click `install.command`** in the unzipped/cloned folder. macOS
   may warn it's from an unidentified developer the first time -- right-click
   it and choose Open instead, once, to allow it. If this folder is a git
   checkout, it also pulls the latest version first.

That's the whole install -- no Terminal typing required. (`./install.sh`
from Terminal still works too, if you'd rather.)

Then fully quit and reopen Fiji. The tool appears as a normal menu item:

**Plugins > ZebrafishQuant > Zebrafish Quant**

No need to open the Script Editor, pick a language, or navigate to a file --
click the menu item like any other Fiji plugin.

If it can't find your Fiji install automatically, point it at the folder
directly from Terminal:

```bash
./install.sh /Applications/Fiji        # or wherever your Fiji folder is
```

### Getting it via git (recommended -- easiest to update)

```bash
git clone https://github.com/marjvilla/zFishEpiFloQuant.git
cd zFishEpiFloQuant
```

Then double-click `install.command` (or run `./install.sh`) as above.

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

The plugin checks for you: its setup screen (the first thing you see when
you run it from the Plugins menu) shows a note if a newer version is on
GitHub, no separate step needed. You can also check any time without
opening Fiji -- **double-click `check_updates.command`**. Either way, if
you have a git checkout, **double-click `install.command`** to pull and
reinstall in one click. If you're on a zip download instead, it'll tell
you to grab a fresh zip (zips don't self-update).

From Terminal, if you have a git checkout:

```bash
git pull
```

That's it. The Fiji menu item is a symlink into this folder, so whatever is
checked out here is what runs -- no re-copying, no re-running `install.sh`.
Just quit and reopen Fiji (or Help > Refresh Menus) to pick up the change.

### Sending a zip to someone directly

If you're the one distributing it (rather than pointing someone at GitHub),
`git clone` from the [GitHub zip download](https://github.com/marjvilla/zFishEpiFloQuant)
button works, but its `check_updates.command` can't tell what version it is.
Use `./make_release_zip.sh` instead -- it bakes in a `VERSION` file so
`check_updates.command` works for the person you send it to as well.

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

## Size + intensity metrics

The dataset CSV has raw stats per channel, but comparing fish of different
sizes needs those normalized. Three ways to get it, all producing the exact
same two files:

- **From inside Fiji** -- click **"Export summary CSVs"** in the control
  panel, any time during or after a session. This is the easiest way; no
  separate tool, no Terminal.
- **Drag `..._dataset.csv` onto `export_derived_metrics.command`** in
  Finder (or double-click it and paste the path when asked) -- for a CSV
  from an older session, or a computer without Fiji.
- From Terminal:
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
install.command             double-click: install/update (macOS)
check_updates.command       double-click: check for a newer version
export_derived_metrics.command  double-click / drag a CSV onto: run the
                                 export below with no Terminal
export_derived_metrics.py  CLI: size/intensity-normalized CSV (run on your
                            computer, after measuring -- see above)
zfquant/
  core.py             measurement + threshold math      (pure, unit-tested)
  manifest.py         fish -> plane mapping + overrides  (pure, unit-tested)
  journal.py          append-only session state          (pure, unit-tested)
  derive.py           size/intensity metrics -- used by both the plugin's
                      "Export summary CSVs" button and the CLI below
                      (pure, unit-tested, Jython/CPython compatible)
  fiji_io.py           every ImageJ/Java call lives here
  workflow.py          the per-fish state machine
  review.py             the interactive setup-time review panel
  ui.py                 the measurement-time control panel
  startup.py            setup dialogs (before review)
  update_check.py       GitHub update check shown on the setup screen
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
