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

if [ -f "$TARGET/audio/manifest.json" ] && [ -f "$TARGET/audio/zargan-cognitive-architecture.mp3" ] && [ -f "$TARGET/segments.json" ]; then
  SOURCE_HASH=$(sha256sum "$ROOT/docs/public/cognitive-architecture/content.json" | awk '{print $1}')
  PUBLISHED_HASH=$(python3 - "$TARGET/audio/manifest.json" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1])).get("source_sha256", ""))
PY
)
  if [ "$SOURCE_HASH" = "$PUBLISHED_HASH" ]; then
    install -d "$TMP/audio"
    cp "$TARGET/audio/zargan-cognitive-architecture.mp3" "$TMP/audio/"
    cp "$TARGET/audio/manifest.json" "$TMP/audio/"
    cp "$TARGET/segments.json" "$TMP/"
    REUSE_AUDIO=1
  else
    REUSE_AUDIO=0
  fi
else
  REUSE_AUDIO=0
fi

if [ "$REUSE_AUDIO" -eq 0 ]; then
python3 "$ROOT/ops/provisioning/public-pages/cognitive-architecture/render_zargan.py"   --source "$ROOT/docs/public/cognitive-architecture/content.json"   --output-dir "$TMP"
fi

install -d "$TARGET"
rsync -a --delete "$TMP/" "$TARGET/"
chmod -R a+rX "$TARGET"
printf '%s\n' "$EXPECTED_SHA" > "$TARGET/GITHUB_SHA"
echo "published source SHA $EXPECTED_SHA -> $TARGET"
