#!/bin/bash
set -euo pipefail

: "${WHATSAPP_PROFILE_DIR_IN_CONTAINER:=/profile}"
start_url=${WHATSAPP_START_URL:-https://web.whatsapp.com/}
case "$start_url" in
  about:blank|https://web.whatsapp.com/) ;;
  *) echo 'unsupported browser start URL' >&2; exit 2 ;;
esac
test -d "$WHATSAPP_PROFILE_DIR_IN_CONTAINER"
test -w "$WHATSAPP_PROFILE_DIR_IN_CONTAINER"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 0700 "$XDG_RUNTIME_DIR"

Xvfb :98 -screen 0 1280x960x24 -nolisten tcp &
xvfb_pid=$!
for _ in $(seq 1 40); do
  test -S /tmp/.X11-unix/X98 && break
  sleep 0.25
done
test -S /tmp/.X11-unix/X98

google-chrome --disable-restore-session-state \
  --no-first-run --no-default-browser-check \
  --user-data-dir="$WHATSAPP_PROFILE_DIR_IN_CONTAINER" \
  --remote-debugging-address=127.0.0.1 --remote-debugging-port=9223 \
  "$start_url" &
chrome_pid=$!
for _ in $(seq 1 120); do
  curl -fsS --max-time 1 http://127.0.0.1:9223/json/version >/dev/null 2>&1 && break
  kill -0 "$chrome_pid"
  sleep 0.5
done
curl -fsS --max-time 2 http://127.0.0.1:9223/json/version >/dev/null

bridge_ip=$(getent ahostsv4 browser-cdp | awk 'NR == 1 {print $1}')
test -n "$bridge_ip"
socat "TCP-LISTEN:9223,bind=${bridge_ip},reuseaddr,fork" TCP:127.0.0.1:9223 &
edge_pid=$!
cleanup() {
  kill "$edge_pid" "$chrome_pid" "$xvfb_pid" 2>/dev/null || true
  wait "$edge_pid" "$chrome_pid" "$xvfb_pid" 2>/dev/null || true
}
trap cleanup EXIT TERM INT
wait -n "$edge_pid" "$chrome_pid" "$xvfb_pid"
exit 1
