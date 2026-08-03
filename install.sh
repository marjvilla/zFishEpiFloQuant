#!/usr/bin/env bash
# Installs Zebrafish Quant as a Fiji menu item (Plugins > ZebrafishQuant),
# without copying any files: it symlinks back at this cloned repo, so
# `git pull` here is the only update step anyone ever needs -- no
# re-copying, no re-running this script, on any machine that has already
# installed it once.
#
# Two DIFFERENT symlink locations on purpose:
#   scripts/Plugins/ZebrafishQuant/Zebrafish_Quant.py  <- just the entry point
#   jars/Lib/zfquant                                    <- the support package
# Fiji's script menu recurses into every folder under scripts/Plugins and
# turns each .py file it finds into its own clickable menu entry. If the
# zfquant/ package sat next to Zebrafish_Quant.py, every internal module
# (core.py, fiji_io.py, ...) would show up as a bogus, individually
# clickable submenu item. jars/Lib is on Jython's import path but is never
# scanned for menu items, so only the real entry point appears in the menu.
#
# Usage:
#   ./install.sh                 # auto-detect Fiji
#   ./install.sh /path/to/Fiji   # point at a specific Fiji install if
#                                 # auto-detect doesn't find yours
#
# macOS / Linux only. For Windows, see the "Manual install" section in
# README.md -- symlinks need mklink /D and admin/dev-mode rights there, which
# isn't worth scripting blind.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Every place Fiji is commonly found on macOS, across both install layouts:
# the classic single-folder "Fiji.app" (scripts/ directly inside it) and the
# newer jaunch layout, where "Fiji.app" is a thin launcher and scripts/ sits
# next to it under a plain "Fiji" folder.
CANDIDATES=(
  "/Applications/Fiji.app"
  "/Applications/Fiji"
  "$HOME/Applications/Fiji.app"
  "$HOME/Applications/Fiji"
  "$HOME/Desktop/Fiji.app"
  "$HOME/Desktop/Fiji"
  "$HOME/Downloads/Fiji.app"
)

find_scripts_plugins() {
  local root="$1"
  if [ -d "$root/scripts/Plugins" ]; then
    echo "$root/scripts/Plugins"
    return 0
  fi
  if [ -d "$root/Fiji.app/scripts/Plugins" ]; then
    echo "$root/Fiji.app/scripts/Plugins"
    return 0
  fi
  return 1
}

TARGET=""

if [ "${1:-}" != "" ]; then
  TARGET="$(find_scripts_plugins "$1" || true)"
  if [ -z "$TARGET" ]; then
    echo "Could not find scripts/Plugins under '$1'." >&2
    echo "Point this at your Fiji folder (the one with 'Fiji.app' inside it," >&2
    echo "or that IS Fiji.app itself)." >&2
    exit 1
  fi
else
  for candidate in "${CANDIDATES[@]}"; do
    if TARGET="$(find_scripts_plugins "$candidate" 2>/dev/null || true)" && [ -n "$TARGET" ]; then
      break
    fi
  done
fi

if [ -z "$TARGET" ]; then
  echo "Could not find a Fiji installation automatically." >&2
  echo "" >&2
  echo "Run this again with the path to your Fiji folder, e.g.:" >&2
  echo "    ./install.sh /Applications/Fiji" >&2
  echo "or  ./install.sh /Applications/Fiji.app" >&2
  exit 1
fi

# TARGET is "<root>/scripts/Plugins" -- <root> is the Fiji root in both
# layouts find_scripts_plugins() checks (classic Fiji.app, or the newer
# jaunch layout where Fiji.app is a thin launcher next to scripts/).
FIJI_ROOT="$(dirname "$(dirname "$TARGET")")"

PLUGIN_DIR="$TARGET/ZebrafishQuant"
LIB_DIR="$FIJI_ROOT/jars/Lib"
mkdir -p "$PLUGIN_DIR" "$LIB_DIR"

# Clean up an old-style install (zfquant symlinked inside the plugin folder
# itself), which used to cause the menu clutter described above.
rm -f "$PLUGIN_DIR/zfquant"

ln -sf "$REPO_DIR/Zebrafish_Quant.py" "$PLUGIN_DIR/Zebrafish_Quant.py"
ln -sf "$REPO_DIR/zfquant" "$LIB_DIR/zfquant"

echo "Installed."
echo "  Fiji root:   $FIJI_ROOT"
echo "  Menu entry:  $PLUGIN_DIR/Zebrafish_Quant.py"
echo "  Library:     $LIB_DIR/zfquant"
echo "  Linked from: $REPO_DIR"
echo ""
echo "Now (fully) quit and reopen Fiji, then look under:"
echo "  Plugins > ZebrafishQuant > Zebrafish Quant"
echo ""
echo "To update later: just 'git pull' in this folder. No need to run this"
echo "script again -- the symlinks always point at whatever is checked out here."
