#!/bin/bash
set -euo pipefail

if [[ "${1:-}" == "observer" ]]; then
  exec node /app/src/observer.js
fi

test -d /var/lib/attention-router/whatsapp-transport
test -w /var/lib/attention-router/whatsapp-transport
socat TCP-LISTEN:9223,bind=127.0.0.1,reuseaddr,fork TCP:browser-cdp:9223 &
proxy_pid=$!
cleanup() {
  kill "$proxy_pid" "$node_pid" 2>/dev/null || true
  wait "$proxy_pid" "$node_pid" 2>/dev/null || true
}
trap cleanup EXIT TERM INT
node /app/src/index.js &
node_pid=$!
wait -n "$proxy_pid" "$node_pid"
exit 1
