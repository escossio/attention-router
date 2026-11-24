#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
stage=${1:?stage}
sha=${2:?expected SHA}
waves=${3:?wave count}
mode=${4:-instrumented}
[[ $mode =~ ^(control|instrumented)$ ]]
[[ $(hostname -s) == ci02 && $stage =~ ^/srv/andy-ci/work/andy-m1-e5\.[A-Za-z0-9]+$ && $sha =~ ^[a-f0-9]{40}$ && $waves =~ ^(1|6)$ ]]
base=/srv/andy-ci
mirror="$base/repos/attention-router.git"
exec 9>"$base/worker.lock"
flock -n 9 || { echo CI_WORKER_BUSY; exit 75; }
job=$(mktemp -d "$stage/waves-$waves.XXXXXXXX")
pg="andy-m1-${sha:0:10}-$$"
cleanup() {
    code=$?
    trap - EXIT
    docker rm -f -v "$pg" >/dev/null 2>&1 || true
    git --git-dir="$mirror" worktree remove --force "$job/tree" >/dev/null 2>&1 || true
    rm -f "$job/db.env"
    printf 'M1_WORKER_EXIT=%s\nM1_JOB=%s\n' "$code" "$job"
    exit "$code"
}
trap cleanup EXIT
printf 'M1_JOB=%s\nM1_SHA=%s\nM1_WAVES=%s\n' "$job" "$sha" "$waves"
git --git-dir="$mirror" fetch --quiet origin "$sha"
[[ $(git --git-dir="$mirror" rev-parse FETCH_HEAD) == "$sha" ]]
git --git-dir="$mirror" worktree add -q --detach "$job/tree" "$sha"
cd "$job/tree"
[[ $(git rev-parse HEAD) == "$sha" && -z $(git status --porcelain) ]]
python3 -B - "$stage/model/manifest.json" ops/reconciliation/model-manifest.json <<'PY'
import json
from pathlib import Path
import sys

staged,expected=(json.loads(Path(item).read_text()) for item in sys.argv[1:])
assert staged==expected, 'Pinned model inventory differs'
PY
bench="$PWD/ops/context-canary/m1"
python3 -B "$bench/overlay-m1.py"
git diff --check
[[ $(git diff --name-only) == tests/integration/test_postgres_owner_scope_registry.py ]]
git diff > "$job/overlay.patch"
sha256sum "$bench/m1_owner.py" "$bench/overlay-m1.py" "$job/overlay.patch" | sed -E 's#  .*/#  #'
abi=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
deps=$( { python3 -V; cat pyproject.toml; } | sha256sum | cut -c1-16 )
venv="$base/envs/py$abi-$deps"
[[ -f "$venv/.ready" ]]
export PATH="$venv/bin:$PATH"
if [[ ! -f "$stage/embedding-libs/.ready" ]]; then
    python -B - "$stage" "$base/work/private-oci-dependencies.5VRNL6CN/wheels" <<'PY'
from hashlib import sha256
from pathlib import Path
import subprocess
import sys

stage, wheels=map(Path,sys.argv[1:])
lock=Path('ops/context-canary/requirements.linux-amd64.lock').read_text().splitlines()
selected=[]
for name in ('onnxruntime','tokenizers','numpy','flatbuffers','coloredlogs','humanfriendly','huggingface_hub','fsspec','hf_xet'):
    wheel,=wheels.glob(f'{name}-*.whl')
    requirement,=[line for line in lock if line.replace('-','_').lower().startswith(name+'==')]
    assert sha256(wheel.read_bytes()).hexdigest() in requirement, name
    selected.append(str(wheel))
target=stage/'embedding-libs'
subprocess.run([sys.executable,'-m','pip','--disable-pip-version-check','install','--quiet','--no-index','--no-deps',
                '--target',str(target),*selected],check=True)
(target/'.ready').touch()
PY
fi
export M1_VECTOR_LIBS="$stage/embedding-libs"
export PYTHONPATH="$PWD:$PWD/tests:$PWD/tests/integration:$bench:$stage/embedding-libs"
export CONVERSATION_VECTOR_MODEL_ROOT="$stage/model" HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export TOKENIZERS_PARALLELISM=false M1_WAVES="$waves" M1_RESULTS="$job/metrics.json"
python -V
image='pgvector/pgvector@sha256:484e838076e04939d27d3e176e91c86eea96236126023be8d3059761122e579f'
docker image inspect "$image" >/dev/null
export M1_SHA="$sha" M1_TREE="$(git rev-parse HEAD^{tree})" M1_DB_IMAGE="$image"
export M1_OWNER_FIXTURE_BLOB="$(git rev-parse HEAD:tests/integration/test_postgres_owner_scope_registry.py)"
export M1_CONDITION="${M1_CONDITION:-fresh_process}" M1_TELEMETRY_MODE="$mode"
if [[ $mode == instrumented ]]; then
    export CONTEXT_PERFORMANCE_ENABLED=true OTEL_TRACING_ENABLED=true
    export M1_TRACE_ENDPOINT="${M1_OTLP_ENDPOINT:-http://127.0.0.1:19418/v1/traces}"
    export OTEL_SERVICE_NAME=attention-router-private-context
else
    export CONTEXT_PERFORMANCE_ENABLED=false OTEL_TRACING_ENABLED=false
fi
export M1_TELEMETRY_DIR="${M1_RUN_ROOT:?experimental run root}"
secret=$(openssl rand -hex 24)
printf 'POSTGRES_DB=attention_router_test\nPOSTGRES_USER=ar_test\nPOSTGRES_PASSWORD=%s\n' "$secret" > "$job/db.env"
docker run --rm -d --name "$pg" --label "andy.test-sha=$sha" --env-file "$job/db.env" \
    --tmpfs /var/lib/postgresql/data:rw,size=2048m -p 127.0.0.1::5432 "$image" -c fsync=off -c synchronous_commit=off >/dev/null
for n in {1..40}; do docker exec "$pg" pg_isready -h 127.0.0.1 -U ar_test -d attention_router_test >/dev/null 2>&1 && break; sleep 1; done
[[ $(docker inspect -f '{{ index .Config.Labels "andy.test-sha" }}' "$pg") == "$sha" ]]
[[ $(docker exec "$pg" psql -XAt -U ar_test -d attention_router_test -c 'select current_database()') == 'attention_router_test' ]]
port=$(docker port "$pg" 5432/tcp | sed -En 's/^127\.0\.0\.1:([0-9]+)$/\1/p')
[[ $port =~ ^[0-9]+$ ]]
export DATABASE_URL="postgresql+psycopg://ar_test:$secret@127.0.0.1:$port/attention_router_test"
export PUBLIC_POSTGRES_TEST_URL="$DATABASE_URL" APP_ENV=test
export ADMIN_AUTH_ENABLED=true ADMIN_TOKEN="$secret" INTERNAL_INGRESS_HMAC_SECRET="$secret"
python -m alembic upgrade head > "$job/migrate.log" 2>&1 || { tail -20 "$job/migrate.log"; exit 1; }
[[ $(docker exec "$pg" psql -XAt -U ar_test -d attention_router_test -c 'select version_num from alembic_version') == '0072_context_index_fence' ]]
cpus=$(python -c 'import os; print(",".join(map(str, sorted(os.sched_getaffinity(0))[:2])))')
[[ $cpus =~ ^[0-9]+,[0-9]+$ ]]
printf 'M1_APP_CPU_AFFINITY=2\n'
taskset -c "$cpus" python -m pytest -o addopts= -m postgres -s -q --tb=short \
 'tests/integration/test_postgres_owner_scope_registry.py::test_owner_enrollment_durable_epoch_e5_http_revocation[True-client-query]' \
 > "$job/test.log" 2>&1 || { tail -n 32 "$job/test.log" | sed -E 's/(postgresql[^: ]*:\/\/[^: ]+:)[^@ ]+@/\1[redacted]@/g'; exit 1; }
grep -E '^M1_WAVE |^M1_METRICS |^M1_REOPEN=|^M1_REVOCATION=|[0-9]+ passed' "$job/test.log"
[[ -f "$M1_RESULTS" ]]
printf 'M1_CI02=PASS\n'
