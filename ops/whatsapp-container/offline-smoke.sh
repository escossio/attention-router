#!/bin/bash
# Run only on an isolated CI worker. No profile, pairing or provider traffic.
set -euo pipefail

scratch=$(mktemp -d)
network="andy-wa-offline-${RANDOM}"
browser="andy-wa-browser-offline-${RANDOM}"
transport="andy-wa-transport-offline-${RANDOM}"
observer="andy-wa-observer-offline-${RANDOM}"
probe="andy-wa-browser-probe-${RANDOM}"
cleanup() {
  docker rm -f "$observer" "$transport" "$browser" "$probe" >/dev/null 2>&1 || true
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
  echo 'offline diagnostic only: Chrome sandbox disabled, empty profile' >&2
  # This disposable CI control isolates Chrome sandbox behavior; never use it in Compose.
  mkdir -p "$scratch/probe-profile"
  sudo chown 1000:1000 "$scratch/probe-profile"
  docker run -d --name "$probe" --network "$network" \
    --security-opt no-new-privileges:true --security-opt seccomp=unconfined \
    --user 1000:1000 --shm-size 1g -e WHATSAPP_START_URL=about:blank \
    -v "$scratch/probe-profile:/profile" --entrypoint /bin/bash \
    andy-whatsapp-browser:ci -lc 'mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"; Xvfb :98 -screen 0 1280x960x24 -nolisten tcp & sleep 1; google-chrome --no-sandbox --disable-restore-session-state --user-data-dir=/profile --remote-debugging-address=127.0.0.1 --remote-debugging-port=9223 about:blank & wait' >/dev/null
  for _ in $(seq 1 20); do
    if docker exec "$probe" curl -fsS --max-time 1 http://127.0.0.1:9223/json/version >/dev/null 2>&1; then
      echo 'no-sandbox diagnostic: CDP reachable' >&2
      break
    fi
    sleep 1
  done
  docker inspect --format 'probe={{.State.Status}} health={{.State.Health.Status}} exit={{.State.ExitCode}}' "$probe" >&2 || true
  docker exec "$probe" sh -c 'ps -eo pid,ppid,stat,wchan:24,args | head -20; ls -la /profile | head -18; cat /proc/net/tcp | head -8' >&2 || true
  docker logs --tail 12 "$probe" >&2 || true
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

live=no
for _ in $(seq 1 40); do
  if docker exec "$transport" curl -fsS --max-time 1 http://127.0.0.1:18103/live >/dev/null 2>&1; then live=yes; break; fi
  sleep 1
done
if [[ "$live" != yes ]]; then
  docker logs --tail 40 "$transport" >&2
  echo 'offline transport candidate failed' >&2
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
for _ in $(seq 1 30); do
  if [[ -s "$scratch/observer/status.json" ]]; then observed=yes; break; fi
  sleep 1
done
if [[ "$observed" != yes ]]; then
  docker logs --tail 40 "$observer" >&2
  echo 'offline observer candidate failed' >&2
  exit 1
fi
python3 - "$scratch/observer/status.json" <<'PY'
import json, sys
state=json.load(open(sys.argv[1], encoding='utf-8'))
assert state['capture_body'] is False
PY
echo 'OFFLINE_WHATSAPP_CONTAINER_SMOKE=PASS'
