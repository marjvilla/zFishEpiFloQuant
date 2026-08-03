#!/bin/bash
# Build a distributable zip of this repo, for sending to someone directly
# instead of pointing them at GitHub. Bakes in a VERSION file recording
# exactly which commit it came from, so check_updates.command can tell a
# zip recipient whether a newer version exists later.
#
# Usage: ./make_release_zip.sh [output.zip]
set -e
cd "$(dirname "$0")"

SHA=$(git rev-parse HEAD)
OUT="${1:-zFishEpiFloQuant.zip}"
WORKDIR=$(mktemp -d)

git archive --format=tar --prefix=zFishEpiFloQuant/ HEAD | (cd "$WORKDIR" && tar -xf -)
echo "$SHA" > "$WORKDIR/zFishEpiFloQuant/VERSION"

rm -f "$OUT"
(cd "$WORKDIR" && zip -rq - zFishEpiFloQuant) > "$OUT"
rm -rf "$WORKDIR"

echo "Wrote $OUT (commit $SHA)"
