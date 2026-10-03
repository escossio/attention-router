#!/bin/sh
set -eu
EXPECTED_SHA=${EXPECTED_SHA:?EXPECTED_SHA is required}
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../../.." && pwd)
ACTUAL_SHA=$(git -C "$ROOT" rev-parse HEAD)
[ "$ACTUAL_SHA" = "$EXPECTED_SHA" ] || { echo "SHA mismatch: $ACTUAL_SHA != $EXPECTED_SHA" >&2; exit 1; }

TARGET=${TARGET_ROOT:-/srv/cognitive.escossio.com/public}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

cp "$ROOT/docs/public/cognitive-architecture/index.html" "$TMP/"
cp "$ROOT/docs/public/cognitive-architecture/style.css" "$TMP/"
cp "$ROOT/docs/public/cognitive-architecture/app.js" "$TMP/"
cp "$ROOT/docs/public/cognitive-architecture/content.json" "$TMP/"

python3 "$ROOT/ops/provisioning/public-pages/cognitive-architecture/render_zargan.py"   --source "$ROOT/docs/public/cognitive-architecture/content.json"   --output-dir "$TMP"

install -d "$TARGET"
rsync -a --delete "$TMP/" "$TARGET/"
chmod -R a+rX "$TARGET"
printf '%s\n' "$EXPECTED_SHA" > "$TARGET/GITHUB_SHA"
echo "published source SHA $EXPECTED_SHA -> $TARGET"
