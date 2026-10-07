#!/bin/bash
# Synthetic profile only. Run on a Docker CI worker, never against live state.
set -euo pipefail

scratch=$(mktemp -d)
name="andy-wa-lifecycle-${RANDOM}"
network="${name}-network"
cleanup() {
  docker rm -f "$name" "${name}-second" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  sudo rm -rf "$scratch"
}
trap cleanup EXIT

mkdir -p "$scratch/profile"
chmod 0711 "$scratch"
sudo chown 1000:1000 "$scratch/profile"
docker network create --internal "$network" >/dev/null

run_browser() {
  docker run -d --name "$name" --hostname andy-whatsapp-browser \
    --network "$network" --network-alias browser-cdp \
    --user 1000:1000 --cap-drop ALL --sysctl net.ipv4.ip_forward=0 \
    --security-opt no-new-privileges:true \
    --security-opt "seccomp=$(pwd)/ops/whatsapp-container/chrome-seccomp.json" \
    --shm-size 1g -e WHATSAPP_START_URL=about:blank \
    -v "$scratch/profile:/profile" andy-whatsapp-browser:ci >/dev/null
}

check_browser() {
  local state writers
  for _ in $(seq 1 60); do
    state=$(docker inspect --format '{{.State.Health.Status}}' "$name")
    [[ "$state" == healthy ]] && break
    [[ $(docker inspect --format '{{.State.Status}}' "$name") == exited ]] && break
    sleep 1
  done
  if [[ "$state" != healthy ]]; then
    docker logs --tail 25 "$name" >&2
    echo 'browser lifecycle health failed' >&2
    exit 1
  fi
  docker exec "$name" curl -fsS http://127.0.0.1:9223/json/version >/dev/null
  [[ $(docker exec "$name" cat /proc/sys/net/ipv4/ip_forward) == 0 ]]
  [[ $(docker inspect --format '{{.RestartCount}}' "$name") == 0 ]]
  writers=$(docker exec "$name" sh -c \
    "ps -eo args | grep '^/usr/bin/google-chrome .*--user-data-dir=/profile ' | wc -l")
  [[ "$writers" == 1 ]]
}

run_browser
check_browser
docker restart "$name" >/dev/null
check_browser
docker stop "$name" >/dev/null
docker start "$name" >/dev/null
check_browser

for _ in $(seq 1 4); do
  old=$(docker inspect --format '{{.Id}}' "$name")
  docker rm -f "$name" >/dev/null
  run_browser
  [[ $(docker inspect --format '{{.Id}}' "$name") != "$old" ]]
  check_browser
done

set +e
docker run --rm --name "${name}-second" --hostname andy-whatsapp-browser \
  --network "$network" --user 1000:1000 --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --security-opt "seccomp=$(pwd)/ops/whatsapp-container/chrome-seccomp.json" \
  -e WHATSAPP_START_URL=about:blank \
  -v "$scratch/profile:/profile" andy-whatsapp-browser:ci \
  >"$scratch/second.log" 2>&1
second_rc=$?
set -e
[[ "$second_rc" -ne 0 ]]
grep -q '^SINGLETON_STATE=ACTIVE reason=operational_lock_held' "$scratch/second.log"
check_browser
echo 'WHATSAPP_BROWSER_LIFECYCLE_SMOKE=PASS'
