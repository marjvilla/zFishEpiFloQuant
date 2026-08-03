#!/bin/bash
# Double-click this in Finder to install (or update) Zebrafish Quant --
# no Terminal typing required. Runs install.sh after pulling the latest
# version, if this folder is a git checkout (a zip download has no
# updates to pull, and just installs whatever is here).
set -e
cd "$(dirname "$0")"

if [ -d .git ]; then
    echo "Checking for updates..."
    git pull --ff-only || echo "(couldn't auto-update -- continuing with what's here)"
    echo
fi

./install.sh

echo
echo "Done. Fully quit and reopen Fiji to pick up the change."
read -r -p "Press Enter to close this window..."
