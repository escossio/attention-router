#!/usr/bin/env bash
# Run only on an authorized disposable CI worker, never on AGT/M0.
set -Eeuo pipefail
sha=${1:?exact source SHA required}
output=${2:?exclusive output directory required}
[[ "$sha" =~ ^[0-9a-f]{40}$ ]]
test "$(git rev-parse HEAD)" = "$sha"
test -z "$(git status --porcelain --untracked-files=no)"
mkdir -p "$output"
output=$(realpath "$output")
base=python@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0
postgres=postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea
name="m2-cert-${sha:0:12}-$$"
image="$name:runtime"
cleanup() {
  docker rm -f "$name-db" >/dev/null 2>&1 || true
  docker network rm "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker build --build-arg PYTHON_IMAGE="$base" --build-arg SOURCE_SHA="$sha" \
  -f experiments/incremental_memory/device/Dockerfile -t "$image" .
test "$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")" = "$sha"
test "$(docker inspect --format '{{.Config.User}}' "$image")" = 10001:10001
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,nodev,size=32m "$image" python -m experiments.incremental_memory.device.image_smoke \
  | tee "$output/smoke.json"
docker run --rm --network none --read-only "$image" python -m pip check > "$output/pip-check.txt"
docker run --rm --network none --read-only "$image" python -m pip freeze --all > "$output/dependencies.txt"
docker run --rm --network none --read-only "$image" python -m pip inspect > "$output/dependencies.json"
docker run --rm --network none --read-only "$image" cat /app/m2-install.json > "$output/install-report.json"
python3 - "$output/install-report.json" "$output/runtime.requirements.lock" <<'PY'
import json
import sys
from pathlib import Path
report = json.loads(Path(sys.argv[1]).read_text())
lines = []
for item in report['install']:
    meta = item['metadata']
    digest = item['download_info']['archive_info']['hashes']['sha256']
    lines.append(f"{meta['name']}=={meta['version']} --hash=sha256:{digest}")
Path(sys.argv[2]).write_text('\n'.join(sorted(lines, key=str.lower)) + '\n')
PY
# Disposable test layer: runtime code is inherited without replacement. Provider
# assertions are patched ONLY in these tests; no test code enters the runtime image.
docker build --build-arg RUNTIME_IMAGE="$image" \
  -f experiments/incremental_memory/device/Dockerfile.certify -t "$name:tests" .
docker pull "$postgres"
docker network create --internal "$name" >/dev/null
docker run -d --name "$name-db" --network "$name" --network-alias postgres \
  --memory 512m --cpus 1 --tmpfs /var/lib/postgresql/data:rw,size=256m \
  -e POSTGRES_USER=ar_test -e POSTGRES_DB=attention_router_test \
  -e POSTGRES_HOST_AUTH_METHOD=trust "$postgres" >/dev/null
# Trust is confined to the disposable internal CI network, never the deployment HBA.
ready=false
for attempt in $(seq 1 30); do
  if docker exec "$name-db" pg_isready -U ar_test -d attention_router_test >/dev/null; then ready=true; break; fi
  sleep 1
done
test "$ready" = true
docker run --rm --network "$name" --read-only --cap-drop ALL --security-opt no-new-privileges \
  --memory 512m --cpus 1 --tmpfs /tmp:rw,size=64m \
  -e PUBLIC_POSTGRES_TEST_URL=postgresql+psycopg://ar_test@postgres/attention_router_test \
  "$name:tests" | tee "$output/integration.txt"
docker save "$image" | gzip -n > "$output/m2-image.docker.tar.gz"
docker inspect --format '{{.Id}}' "$image" > "$output/image-config.digest"
printf '%s\n' "$sha" > "$output/source.sha"
printf '%s\n' "$base" > "$output/python-base.digest"
printf '%s\n' "$postgres" > "$output/postgres-base.digest"
(cd "$output" && sha256sum m2-image.docker.tar.gz dependencies.json dependencies.txt > SHA256SUMS)
printf 'M2_IMAGE_CERTIFICATION=PASS\n'
