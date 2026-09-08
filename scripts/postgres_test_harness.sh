#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG_IMAGE="${POSTGRES_TEST_IMAGE:-postgres:16-alpine}"
RUNNER_IMAGE="${POSTGRES_TEST_RUNNER_IMAGE:-attention-router-test-runner:10-5bc}"
PG="attention-router-pg-test-${RANDOM}-${RANDOM}"; RUNNER="attention-router-test-runner-${RANDOM}-${RANDOM}"; NETWORK="attention-router-test-net-${RANDOM}-${RANDOM}"
USER_NAME="ar_test"; DB_NAME="attention_router_test"; PASSWORD="$(openssl rand -hex 24)"; TARGET="${POSTGRES_TEST_TARGET:-head}"
cleanup() { docker rm -f "$RUNNER" "$PG" >/dev/null 2>&1 || true; docker network rm "$NETWORK" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM
docker network create "$NETWORK" >/dev/null
BUILD_SHA="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
docker build --quiet --build-arg ATTENTION_ROUTER_BUILD_SHA="$BUILD_SHA" -t "$RUNNER_IMAGE" "$ROOT_DIR" >/dev/null
docker run -d --name "$PG" --network "$NETWORK" --network-alias postgres -e POSTGRES_USER="$USER_NAME" -e POSTGRES_PASSWORD="$PASSWORD" -e POSTGRES_DB="$DB_NAME" --health-cmd="pg_isready -U $USER_NAME -d $DB_NAME" --health-interval=1s --health-timeout=2s --health-retries=30 "$PG_IMAGE" >/dev/null
for _ in $(seq 1 60); do [ "$(docker inspect -f '{{.State.Health.Status}}' "$PG" 2>/dev/null || true)" = healthy ] && break; sleep 1; done
[ "$(docker inspect -f '{{.State.Health.Status}}' "$PG")" = healthy ] || exit 1
case "$TARGET" in 0023|rehearsal|zero|legacy) REVISION=0023_human_execution_auth ;; 0025|head|newhea) REVISION=head ;; *) echo "Unsupported POSTGRES_TEST_TARGET: $TARGET" >&2; exit 2 ;; esac
DATABASE_URL="postgresql+psycopg://${USER_NAME}:${PASSWORD}@postgres:5432/${DB_NAME}"
run_runner() { docker run --rm --name "$RUNNER" --network "$NETWORK" -e DATABASE_URL="$DATABASE_URL" -e ADMIN_AUTH_ENABLED=false -e META_WEBHOOK_DISPATCH_ENABLED=false -e INTERNAL_INGRESS_HMAC_SECRET="test-only-harness-secret-012345678901234567890123" "$RUNNER_IMAGE" "$@"; }
run_runner python -m alembic upgrade "$REVISION"
if [ "$TARGET" = zero ]; then
  run_runner python -m alembic upgrade 0024_execution_intent
  run_runner python -m alembic upgrade 0025_human_auth_execution_intent
  run_runner python -c 'from sqlalchemy import create_engine,text; import os; e=create_engine(os.environ["DATABASE_URL"]); c=e.connect(); assert c.execute(text("select count(*) from human_execution_authorizations")).scalar()==0; assert c.execute(text("select count(*) from execution_intents")).scalar()==0'
fi
if [ "$TARGET" = newhea ]; then
  run_runner python -c 'from attention_router.infrastructure.db import SessionLocal; from attention_router.infrastructure.models import ExecutionIntentRow; from attention_router.platform.human_execution_authorization import prepare; from datetime import datetime,UTC; from uuid import uuid4; s=SessionLocal(); i=ExecutionIntentRow(id=str(uuid4()),idempotency_key=str(uuid4()),scope={"case":"downgrade"},scope_fingerprint=uuid4().hex,provenance={},state="FROZEN",created_at=datetime.now(UTC),frozen_at=datetime.now(UTC)); s.add(i); s.flush(); prepare(s,execution_intent_id=i.id,expected_approver="owner",ttl_seconds=300,correlation_id=str(uuid4())); s.commit()'
  set +e
  run_runner python -m alembic downgrade 0023_human_execution_auth
  result=$?
  set -e
  [ "$result" -ne 0 ] || exit 1
fi
if [ "$TARGET" = legacy ]; then
  run_runner python -c 'from sqlalchemy import create_engine,text; import os; from datetime import datetime,timezone,timedelta; e=create_engine(os.environ["DATABASE_URL"]); n=datetime.now(timezone.utc); c=e.connect(); tx=c.begin(); c.execute(text("insert into human_execution_authorizations (id,scope,scope_fingerprint,expected_approver,approval_channel,state,issued_at,expires_at,correlation_id,created_at,updated_at) values (:id,:scope,:fp,:approver,:channel,:state,:issued,:expires,:corr,:created,:updated)"),{"id":"legacy-test-auth","scope":"{}","fp":"legacy-test-fingerprint","approver":"owner","channel":"meta_whatsapp_interactive","state":"PREPARED","issued":n,"expires":n+timedelta(seconds=300),"corr":"legacy-test-correlation","created":n,"updated":n}); tx.commit()'
  run_runner python -m alembic upgrade 0024_execution_intent
  set +e
  run_runner python -m alembic upgrade 0025_human_auth_execution_intent
  result=$?
  set -e
  [ "$result" -ne 0 ] || exit 1
  run_runner python -c 'from sqlalchemy import create_engine,text; import os; e=create_engine(os.environ["DATABASE_URL"]); c=e.connect(); assert c.execute(text("select version_num from alembic_version")).scalar()=="0024_execution_intent"; assert c.execute(text("select count(*) from human_execution_authorizations")).scalar()==1; assert c.execute(text("select count(*) from execution_intents")).scalar()==0; assert not any(x["name"]=="execution_intent_id" for x in __import__("sqlalchemy").inspect(c).get_columns("human_execution_authorizations"))'
fi
if [ "$TARGET" = rehearsal ]; then
  run_runner python -m alembic upgrade 0024_execution_intent
  run_runner python -m alembic upgrade 0025_human_auth_execution_intent
  run_runner python -m alembic downgrade 0024_execution_intent
  run_runner python -m alembic downgrade 0023_human_execution_auth
  run_runner python -m alembic upgrade 0025_human_auth_execution_intent
fi
if [ "${POSTGRES_TEST_RUN:-1}" = 1 ]; then
  run_runner python -m alembic current; run_runner python -m alembic heads; run_runner python -m alembic history
  run_runner python -m alembic --version; run_runner python -m pytest --version; run_runner ruff check .; run_runner python -m pytest ${POSTGRES_TEST_SELECTOR:--m postgres} -q
fi
