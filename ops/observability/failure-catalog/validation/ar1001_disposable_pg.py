#!/usr/bin/env python3
"""AR-1001 disposable PG proof. Execute only on a designated CI Docker worker.

Creates one isolated, ephemeral PostgreSQL container with no network, ports, or
persistent storage. Real runtime DB/data/credentials are never contacted.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4


def invoke(cmd: list[str], *, data: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, input=data, text=True, capture_output=True, check=check, timeout=35)


def fixture_sql() -> str:
    lines = [
        "CREATE ROLE ar_readonly_test;",
        "CREATE TABLE inbound_events (interaction_id text, source text, received_at timestamptz, processed_at timestamptz, status text, payload jsonb);",
        "CREATE TABLE agent_decisions (id text, interaction_id text, created_at timestamptz, decision_type text, status text);",
        "CREATE TABLE outbox_messages (id text, interaction_id text, created_at timestamptz, status text, completed_at timestamptz, destination text, action_type text);",
        "CREATE TABLE conversation_response_grace_windows (id text, anchor_interaction_id text, source text, opened_at timestamptz, state text);",
        "CREATE TABLE audit_events (event_type text, created_at timestamptz, interaction_id text);",
    ]
    for i in range(1, 7):
        actor = "'synthetic@lid'" if i % 2 else "'synthetic@c.us'"
        processed = "NULL" if i == 6 else "now() - interval '3 minutes'"
        lines.append(
            f"INSERT INTO inbound_events VALUES ('s{i}','wwebjs',now()-interval '4 minutes',{processed},"
            f"'PROCESSED',jsonb_build_object('actor_id',{actor}));"
        )
    # Duplicate inbound row in the same interaction: row counts differ from distinct interactions.
    lines.append(
        "INSERT INTO inbound_events VALUES ('s1','wwebjs',now()-interval '4 minutes',"
        "now()-interval '2 minutes','PROCESSED','{\"actor_id\":\"synthetic@lid\"}');"
    )
    # Must not be selected: old input, group conversation and another source.
    lines.extend([
        "INSERT INTO inbound_events VALUES ('old','wwebjs',now()-interval '8 hours',now()-interval '8 hours','PROCESSED','{\"actor_id\":\"synthetic@lid\"}');",
        "INSERT INTO inbound_events VALUES ('group','wwebjs',now()-interval '4 minutes',now()-interval '4 minutes','PROCESSED','{\"actor_id\":\"synthetic@g.us\"}');",
        "INSERT INTO inbound_events VALUES ('other','email',now()-interval '4 minutes',now()-interval '4 minutes','PROCESSED','{\"actor_id\":\"synthetic@lid\"}');",
    ])
    for i in range(1, 6):
        lines.append(
            f"INSERT INTO agent_decisions VALUES ('d{i}','s{i}',now()-interval '2 minutes','INSUFFICIENT_CONTEXT','DRY_RUN');"
        )
    lines.append(
        "INSERT INTO agent_decisions VALUES ('d6','s6',now()-interval '2 minutes','APPROVED','EXECUTED');"
    )
    for i in range(1, 6):
        lines.append(f"INSERT INTO audit_events VALUES ('behavior.unavailable',now()-interval '2 minutes','s{i}');")
        lines.append(f"INSERT INTO audit_events VALUES ('conversation.blocked',now()-interval '2 minutes','s{i}');")
    lines.extend([
        "INSERT INTO outbox_messages VALUES ('o1','s6',now()-interval '1 minute','DONE',now()-interval '1 minute','wwebjs','agent_execution_text');",
        "INSERT INTO outbox_messages VALUES ('old-o','old',now()-interval '8 hours','DONE',now()-interval '8 hours','wwebjs','agent_execution_text');",
        "INSERT INTO outbox_messages VALUES ('group-o','group',now()-interval '1 minute','DONE',now()-interval '1 minute','wwebjs','agent_execution_text');",
    ])
    for i in range(1, 7):
        state = "RELEASED" if i <= 4 else ("CANCELED" if i == 5 else "OPEN")
        lines.append(
            f"INSERT INTO conversation_response_grace_windows VALUES ('g{i}','s{i}','wwebjs',now()-interval '3 minutes','{state}');"
        )
    lines.extend([
        "GRANT USAGE ON SCHEMA public TO ar_readonly_test;",
        "GRANT SELECT ON ALL TABLES IN SCHEMA public TO ar_readonly_test;",
    ])
    return "\n".join(lines)+"\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sql", required=True, type=Path)
    args = parser.parse_args()
    source = args.sql.read_text(encoding="utf-8")
    if "BEGIN TRANSACTION READ ONLY;" not in source or "SET LOCAL statement_timeout" not in source:
        raise SystemExit("REFUSE_NON_READONLY_SCRIPT")
    name = "ar1001-pg-proof-" + uuid4().hex[:10]
    expected = [7, 6, 6, 5, 1, 4, 1]
    print("DISPOSABLE_POSTGRES_START", flush=True)
    try:
        invoke([
            "docker", "run", "-d", "--name", name, "--network", "none",
            "--user", "70:70",
            "--tmpfs", "/var/lib/postgresql/data:rw,size=192m,uid=70,gid=70,mode=0700",
            "--tmpfs", "/var/run/postgresql:rw,size=16m,uid=70,gid=70,mode=0770",
            "--memory", "384m", "--cpus", "1", "--pids-limit", "96",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "postgres:16-alpine",
            "-c", "listen_addresses=",
        ])
        exec_prefix = ["docker", "exec", "-i", "-u", "postgres", name,
                       "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "postgres", "-Atq"]
        for _ in range(25):
            r = invoke(["docker", "exec", name, "pg_isready", "-q"], check=False)
            if r.returncode == 0:
                break
            time.sleep(.3)
        else:
            raise RuntimeError("DISPOSABLE_PG_NOT_READY")
        invoke(exec_prefix, data=fixture_sql())
        r = invoke(exec_prefix, data="SET ROLE ar_readonly_test;\n" + source)
        rows = [line.strip() for line in r.stdout.splitlines() if line.strip()]
        if len(rows) != 1:
            raise AssertionError(f"expected one result row, got {len(rows)}")
        observed = [int(v) for v in rows[0].split("|")]
        if len(observed) == 14:
            legacy, observed = observed[:7], observed[7:]
            if legacy[:4] != [6, 5, 5, 0] or any(ts <= 0 for ts in legacy[4:]):
                raise AssertionError(f"unexpected seven legacy counters: {legacy}")
            print("AR1001_LEGACY_7_FIELDS_ASSERT_PASS", flush=True)
        if observed != expected:
            raise AssertionError(f"expected {expected} got {observed}")
        print(f"AR1001_COUNT_ASSERT_PASS expected={expected} observed={observed}", flush=True)
        # Explicitly prove DB role cannot update the existing synthetic input.
        forbidden = invoke(exec_prefix, data="SET ROLE ar_readonly_test;\nUPDATE inbound_events SET status='FAKE';\n", check=False)
        if forbidden.returncode == 0 or "permission denied" not in forbidden.stderr.lower():
            raise AssertionError("READONLY_ROLE_CANNOT_UPDATE_ASSERT_FAILED")
        print("AR1001_READONLY_ROLE_ASSERT_PASS", flush=True)
        # A monitored system with no matching direct events must emit measured
        # zeros; synthetic data is removed ONLY inside the disposable database.
        invoke(exec_prefix, data=(
            "TRUNCATE TABLE audit_events, agent_decisions, outbox_messages, "
            "conversation_response_grace_windows, inbound_events;\n"
        ))
        empty = invoke(exec_prefix, data="SET ROLE ar_readonly_test;\n" + source)
        empty_rows = [line.strip() for line in empty.stdout.splitlines() if line.strip()]
        if len(empty_rows) != 1:
            raise AssertionError("EMPTY_CASE_NOT_SINGLE_ROW")
        empty_values = [int(v) for v in empty_rows[0].split("|")]
        if len(empty_values) not in (7, 14) or any(empty_values):
            raise AssertionError("EMPTY_CASE_NOT_ZERO")
        print("AR1001_EMPTY_WINDOW_ASSERT_PASS", flush=True)

        print("AR1001_DISPOSABLE_PG_PASS", flush=True)
        return 0
    finally:
        result=invoke(["docker","rm","-f",name], check=False)
        if result.returncode != 0:
            print("CLEANUP_FAILED", file=sys.stderr)
            raise RuntimeError("DISPOSABLE_CONTAINER_CLEANUP_FAILED")


if __name__ == "__main__":
    raise SystemExit(main())
