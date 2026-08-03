# Zebrafish Quant

A Fiji/ImageJ tool for quantifying fluorescent signal (GFP, RFP, ...) in
zebrafish epi-fluorescence images, normalized against eye area from
brightfield.

```
Corrected Intensity = FL Integrated Density - (Mean Background Intensity x FL Area)
```

## Install (one time, per computer)

Requires [Fiji](https://fiji.sc) already installed, and `git`.

```bash
git clone <this repo's URL>
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

Symlink (not copy) `Zebrafish_Quant.py` and the `zfquant/` folder from this
repo into `<your Fiji folder>/scripts/Plugins/ZebrafishQuant/`.

- **macOS/Linux**, by hand:
  ```bash
  mkdir -p "/path/to/Fiji/scripts/Plugins/ZebrafishQuant"
  ln -s "$(pwd)/Zebrafish_Quant.py" "/path/to/Fiji/scripts/Plugins/ZebrafishQuant/Zebrafish_Quant.py"
  ln -s "$(pwd)/zfquant" "/path/to/Fiji/scripts/Plugins/ZebrafishQuant/zfquant"
  ```
- **Windows** (Command Prompt as Administrator, or with Developer Mode on):
  ```
  mkdir "C:\Fiji.app\scripts\Plugins\ZebrafishQuant"
  mklink "C:\Fiji.app\scripts\Plugins\ZebrafishQuant\Zebrafish_Quant.py" "C:\path\to\repo\Zebrafish_Quant.py"
  mklink /D "C:\Fiji.app\scripts\Plugins\ZebrafishQuant\zfquant" "C:\path\to\repo\zfquant"
  ```

Symlinks matter here, not copies: it's what makes updating later a single
`git pull` with nothing else to redo.

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

## Repo layout

```
Zebrafish_Quant.py   thin Fiji entry point
zfquant/
  core.py             measurement + threshold math      (pure, unit-tested)
  manifest.py         fish -> plane mapping + overrides  (pure, unit-tested)
  journal.py          append-only session state          (pure, unit-tested)
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
