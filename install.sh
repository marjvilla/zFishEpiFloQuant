#!/usr/bin/env bash
# Installs Zebrafish Quant as a Fiji menu item (Plugins > ZebrafishQuant),
# without copying any files: it creates two symlinks pointing back at this
# cloned repo, so `git pull` here is the only update step anyone ever needs
# -- no re-copying, no re-running this script, on any machine that has
# already installed it once.
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

PLUGIN_DIR="$TARGET/ZebrafishQuant"
mkdir -p "$PLUGIN_DIR"

ln -sf "$REPO_DIR/Zebrafish_Quant.py" "$PLUGIN_DIR/Zebrafish_Quant.py"
ln -sf "$REPO_DIR/zfquant" "$PLUGIN_DIR/zfquant"

echo "Installed."
echo "  Fiji scripts folder: $TARGET"
echo "  Linked from:         $REPO_DIR"
echo ""
echo "Now (fully) quit and reopen Fiji, then look under:"
echo "  Plugins > ZebrafishQuant > Zebrafish Quant"
echo ""
echo "To update later: just 'git pull' in this folder. No need to run this"
echo "script again -- the symlinks always point at whatever is checked out here."
