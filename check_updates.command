#!/bin/bash
# Double-click this in Finder to check whether a newer version of
# Zebrafish Quant is available on GitHub -- no Terminal typing required.
cd "$(dirname "$0")"

REPO="marjvilla/zFishEpiFloQuant"
REPO_URL="https://github.com/$REPO"

if [ -d .git ]; then
    # A git checkout: compare against the real remote directly.
    git fetch origin main -q 2>/dev/null
    LOCAL_SHA=$(git rev-parse HEAD 2>/dev/null)
    REMOTE_SHA=$(git rev-parse origin/main 2>/dev/null)
    if [ -z "$REMOTE_SHA" ]; then
        echo "Couldn't reach GitHub to check for updates (no internet?)."
    elif [ "$LOCAL_SHA" == "$REMOTE_SHA" ]; then
        echo "You're up to date."
    else
        echo "An update is available."
        echo "Double-click install.command again to pull and install it,"
        echo "or run: git pull"
    fi
else
    # A zip download: no .git to compare against, so check the version
    # baked into this zip (VERSION, written when the zip was made) against
    # the latest commit on GitHub.
    LOCAL_SHA=""
    if [ -f VERSION ]; then
        LOCAL_SHA=$(cat VERSION)
    fi
    REMOTE_SHA=$(curl -fsS "https://api.github.com/repos/$REPO/commits/main" 2>/dev/null \
        | python3 -c "import json,sys
try:
    print(json.load(sys.stdin).get('sha',''))
except Exception:
    pass" 2>/dev/null)

    if [ -z "$REMOTE_SHA" ]; then
        echo "Couldn't reach GitHub to check for updates (no internet?)."
    elif [ -z "$LOCAL_SHA" ]; then
        echo "This copy doesn't know its own version (no VERSION file),"
        echo "so it can't tell if it's current. Get the latest at:"
        echo "  $REPO_URL"
    elif [ "$LOCAL_SHA" == "$REMOTE_SHA" ]; then
        echo "You're up to date."
    else
        echo "A newer version is available at:"
        echo "  $REPO_URL"
        echo "Ask for a fresh copy, or clone it with git for automatic updates."
    fi
fi

echo
read -r -p "Press Enter to close this window..."
