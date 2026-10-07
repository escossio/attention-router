#!/bin/bash
# Run only on an isolated CI worker. No profile, pairing or provider traffic.
set -euo pipefail

scratch=$(mktemp -d)
network="andy-wa-offline-${RANDOM}"
browser="andy-wa-browser-offline-${RANDOM}"
transport="andy-wa-transport-offline-${RANDOM}"
observer="andy-wa-observer-offline-${RANDOM}"
cleanup() {
  docker rm -f "$observer" "$transport" "$browser" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  sudo rm -rf "$scratch"
}
trap cleanup EXIT

mkdir -p "$scratch"/{profile,spool,media,observer}
chmod 0711 "$scratch"
sudo chown -R 1000:1000 "$scratch"/{profile,spool,media,observer}
docker network create --internal "$network" >/dev/null

docker run -d --name "$browser" --network "$network" \
  --network-alias browser-cdp --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --security-opt "seccomp=$(pwd)/ops/whatsapp-container/chrome-seccomp.json" \
  --user 1000:1000 \
  --shm-size 1g -e WHATSAPP_START_URL=about:blank \
  -v "$scratch/profile:/profile" andy-whatsapp-browser:ci >/dev/null

healthy=no
for attempt in $(seq 1 120); do
  state=$(docker inspect --format '{{.State.Health.Status}}' "$browser")
  if [[ "$state" == healthy ]]; then healthy=yes; break; fi
  if [[ "$state" == unhealthy ]]; then break; fi
  if [[ "$attempt" == 10 ]]; then
    echo 'offline browser diagnostic at 10 seconds' >&2
    docker exec "$browser" sh -c 'ps -eo pid,ppid,stat,args | head -35; ls -la /profile | head -35; cat /proc/net/tcp | head -15' >&2 || true
  fi
  sleep 1
done
if [[ "$healthy" != yes ]]; then
  docker inspect --format 'browser={{.State.Status}} health={{.State.Health.Status}} exit={{.State.ExitCode}}' "$browser" >&2 || true
  docker exec "$browser" sh -c 'ps -eo pid,ppid,stat,args | head -35; ls -la /profile | head -35; cat /proc/net/tcp | head -15' >&2 || true
  docker logs --tail 40 "$browser" >&2
  echo 'offline browser candidate failed' >&2
  exit 1
fi

docker run -d --name "$transport" --network "$network" \
  --cap-drop ALL --security-opt no-new-privileges:true --user 1000:1000 \
  -e BROWSER_DEBUG_URL=http://127.0.0.1:9223 \
  -e LOCAL_TRANSPORT_HTTP_HOST=127.0.0.1 \
  -e LOCAL_TRANSPORT_HTTP_PORT=18103 \
  -e INBOUND_FORWARD_ENABLED=false -e EXTERNAL_DELIVERY_ENABLED=false \
  -e LOCAL_INBOUND_SPOOL_DIR=/var/lib/attention-router/whatsapp-transport \
  -v "$scratch/spool:/var/lib/attention-router/whatsapp-transport" \
  -v "$scratch/media:/var/lib/attention-router/whatsapp-media" \
  andy-whatsapp-transport:ci >/dev/null

proxy_ready=no
for _ in $(seq 1 20); do
  if docker exec "$transport" curl -fsS --max-time 1 http://127.0.0.1:9223/json/version >/dev/null 2>&1; then proxy_ready=yes; break; fi
  sleep 1
done
sleep 3
if [[ "$proxy_ready" != yes || "$(docker inspect --format '{{.State.Running}}' "$transport")" != true ]]; then
  docker logs --tail 40 "$transport" >&2
  echo 'offline transport or local CDP proxy failed' >&2
  exit 1
fi

docker run -d --name "$observer" --network "container:$browser" \
  --cap-drop ALL --security-opt no-new-privileges:true --user 1000:1000 \
  -e ATTENTION_WHATSAPP_OBSERVER_BROWSER_URL=http://127.0.0.1:9223 \
  -e ATTENTION_WHATSAPP_OBSERVER_OUTPUT_DIR=/var/lib/attention-router/whatsapp-observer \
  -e ATTENTION_WHATSAPP_OBSERVER_CAPTURE_BODY=false \
  -v "$scratch/observer:/var/lib/attention-router/whatsapp-observer" \
  andy-whatsapp-transport:ci observer >/dev/null

observed=no
for _ in $(seq 1 20); do
  if docker logs "$observer" 2>&1 | grep -F '"event":"observer_page_selection_blocked","reason":"ZERO_CONNECTED_PAGES"' >/dev/null; then observed=yes; break; fi
  sleep 1
done
if [[ "$observed" != yes || "$(docker inspect --format '{{.State.Running}}' "$observer")" != true ]] \
    || ! docker logs "$observer" 2>&1 | grep -F '"capture_body":false' >/dev/null; then
  docker logs --tail 40 "$observer" >&2
  echo 'offline observer candidate failed' >&2
  exit 1
fi
echo 'OFFLINE_WHATSAPP_CONTAINER_SMOKE=PASS'
