#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/usr/local/lib/andy-github-app")
from probe import CONFIG, INSTALLATION_CONFIG, load_env, make_jwt, request_json

DB_PATH = Path("/var/lib/andy-github-app/events.sqlite3")
CHECK_NAME = "distributed-postgres"
TARGET_REPO = "escossio/attention-router"
CI_RUN = "/usr/local/lib/andy-ci/bin/andy-ci-distributed"
CI_REPROFILE = "/usr/local/lib/andy-ci/bin/andy-ci-reprofile"
ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
DEBOUNCE_SECONDS = 5
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def installation_token() -> str:
    cfg = load_env(CONFIG)
    install = load_env(INSTALLATION_CONFIG)
    jwt = make_jwt(
        cfg["GITHUB_APP_CLIENT_ID"],
        cfg["GITHUB_APP_PRIVATE_KEY_PATH"],
    )
    installation_id = int(install["GITHUB_APP_INSTALLATION_ID"])
    status, access = request_json(
        f"/app/installations/{installation_id}/access_tokens",
        jwt,
        method="POST",
        body={},
    )
    if status != 201:
        raise RuntimeError("installation token creation failed")
    permissions = access.get("permissions") or {}
    if permissions.get("checks") != "write":
        raise RuntimeError("CHECKS_WRITE_NOT_GRANTED")
    return access["token"]
def ensure_tables() -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS distributed_postgres_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            delivery_id TEXT,
            repository TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            head_sha TEXT NOT NULL,
            state TEXT NOT NULL,
            check_run_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            detail TEXT,
            UNIQUE(repository, pr_number, head_sha)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS distributed_postgres_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        db.commit()


def bootstrap_cursor() -> None:
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            "SELECT value FROM distributed_postgres_meta WHERE key='delivery_cursor'"
        ).fetchone()
        if row is not None:
            return
        max_rowid = db.execute("SELECT COALESCE(MAX(rowid),0) FROM deliveries").fetchone()[0]
        db.execute(
            "INSERT INTO distributed_postgres_meta(key,value) VALUES('delivery_cursor',?)",
            (str(max_rowid),),
        )
        db.commit()
def enqueue_new_deliveries() -> int:
    created = 0
    with sqlite3.connect(DB_PATH) as db:
        cursor = int(db.execute(
            "SELECT value FROM distributed_postgres_meta WHERE key='delivery_cursor'"
        ).fetchone()[0])
        rows = db.execute(
            """SELECT rowid, delivery_id, repository, action, pr_number, head_sha
               FROM deliveries WHERE rowid > ? ORDER BY rowid""",
            (cursor,),
        ).fetchall()
        last = cursor
        stamp = now_iso()
        for rowid, delivery_id, repo, action, pr_number, sha in rows:
            last = rowid
            if (
                repo == TARGET_REPO
                and action in ACTIONS
                and pr_number is not None
                and isinstance(sha, str)
                and re.fullmatch(r"[0-9a-fA-F]{40}", sha)
            ):
                cur = db.execute(
                    """INSERT OR IGNORE INTO distributed_postgres_jobs
                       (delivery_id,repository,pr_number,head_sha,state,created_at,updated_at)
                       VALUES (?,?,?,?, 'PENDING', ?,?)""",
                    (delivery_id, repo, pr_number, sha.lower(), stamp, stamp),
                )
                created += cur.rowcount
                if cur.rowcount:
                    db.execute(
                        """UPDATE distributed_postgres_jobs
                           SET state='STALE', detail='superseded before execution', updated_at=?
                           WHERE repository=? AND pr_number=? AND state='PENDING'
                             AND head_sha<>?""",
                        (stamp, repo, pr_number, sha.lower()),
                    )
        if last != cursor:
            db.execute(
                "UPDATE distributed_postgres_meta SET value=? WHERE key='delivery_cursor'",
                (str(last),),
            )
        db.commit()
    return created
def claim_job() -> dict | None:
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """SELECT * FROM distributed_postgres_jobs
               WHERE state='PENDING' ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        if row is None:
            db.commit()
            return None
        stamp = now_iso()
        db.execute(
            "UPDATE distributed_postgres_jobs SET state='RUNNING',updated_at=? WHERE id=?",
            (stamp, row["id"]),
        )
        db.commit()
        return dict(row)


def finish_job(job_id: int, state: str, detail: str, check_run_id: int | None) -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """UPDATE distributed_postgres_jobs
               SET state=?,detail=?,check_run_id=?,updated_at=? WHERE id=?""",
            (state, detail[:2000], check_run_id, now_iso(), job_id),
        )
        db.commit()
def current_pr(token: str, repo: str, pr_number: int) -> dict:
    status, pr = request_json(f"/repos/{repo}/pulls/{pr_number}", token)
    if status != 200:
        raise RuntimeError(f"PR_LOOKUP_FAILED status={status}")
    return pr


def create_check(token: str, repo: str, sha: str, pr_number: int) -> int:
    status, check = request_json(
        f"/repos/{repo}/check-runs",
        token,
        method="POST",
        body={
            "name": CHECK_NAME,
            "head_sha": sha,
            "status": "in_progress",
            "started_at": now_iso(),
            "output": {
                "title": "Distributed PostgreSQL gate running",
                "summary": f"PR #{pr_number} exact SHA {sha} CI01/CI02/CI03",
            },
        },
    )
    if status != 201:
        raise RuntimeError(f"CHECK_CREATE_FAILED status={status}")
    return int(check["id"])


def complete_check(token: str, repo: str, check_id: int, conclusion: str, title: str, summary: str) -> None:
    status, _ = request_json(
        f"/repos/{repo}/check-runs/{check_id}",
        token,
        method="PATCH",
        body={
            "status": "completed",
            "conclusion": conclusion,
            "completed_at": now_iso(),
            "output": {"title": title[:255], "summary": summary[:60000]},
        },
    )
    if status != 200:
        raise RuntimeError(f"CHECK_UPDATE_FAILED status={status}")
def run_command(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=1800,
        check=False,
    )


def extract_summary(output: str) -> dict | None:
    matches = re.findall(r"^CI_DISTRIBUTED_SUMMARY=(.+)$", output, re.M)
    if not matches:
        return None
    path = Path(matches[-1].strip())
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def render_summary(summary: dict | None, *, reprofiled: bool) -> str:
    if not summary:
        return "Distributed PostgreSQL gate completed without a readable summary."
    workers = summary.get("workers") or {}
    parts = [
        f"Exact SHA: {summary.get('sha')}",
        f"Wall time: {summary.get('wall_seconds', '?')} s",
        f"Tests passed: {summary.get('total_passed_tests', '?')}",
        f"Automatic reprofile: {'yes' if reprofiled else 'no'}",
        "",
        "| Worker | Status | Duration | Tests | Files |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for name in ("ci01", "ci02", "ci03"):
        data = workers.get(name) or {}
        parts.append(
            f"| {name.upper()} | {data.get('status','?')} | "
            f"{data.get('duration_seconds','?')} s | "
            f"{data.get('passed_tests','?')} | {data.get('target_files','?')} |"
        )
    return "\n".join(parts)
def execute_exact_sha(repo: str, pr_number: int, sha: str) -> tuple[str, str, int | None]:
    token = installation_token()
    pr = current_pr(token, repo, pr_number)
    current_sha = ((pr.get("head") or {}).get("sha") or "").lower()
    if pr.get("state") != "open" or current_sha != sha.lower():
        return "STALE", "PR is closed or head SHA moved before execution.", None

    check_id = create_check(token, repo, sha, pr_number)
    reprofiled = False
    try:
        result = run_command([CI_RUN, sha, "postgres"])
        if result.returncode == 66:
            reprofiled = True
            repro = run_command([CI_REPROFILE, sha])
            if repro.returncode != 0:
                detail = "Automatic reprofile failed.\n\n" + repro.stdout[-12000:]
                complete_check(
                    token, repo, check_id, "failure",
                    "Distributed PostgreSQL reprofile failed", detail,
                )
                return "FAIL", detail, check_id
            result = run_command([CI_RUN, sha, "postgres"])

        summary = extract_summary(result.stdout)
        if result.returncode == 0 and summary and summary.get("status") == "PASS":
            text = render_summary(summary, reprofiled=reprofiled)
            complete_check(
                token, repo, check_id, "success",
                "Distributed PostgreSQL gate passed", text,
            )
            return "PASS", text, check_id

        detail = render_summary(summary, reprofiled=reprofiled)
        detail += "\n\n" + result.stdout[-12000:]
        complete_check(
            token, repo, check_id, "failure",
            "Distributed PostgreSQL gate failed", detail,
        )
        return "FAIL", detail, check_id
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        try:
            complete_check(
                token, repo, check_id, "failure",
                "Distributed PostgreSQL gate error", detail,
            )
        finally:
            return "FAIL", detail, check_id
def run_job(job: dict) -> None:
    time.sleep(DEBOUNCE_SECONDS)
    state, detail, check_id = execute_exact_sha(
        job["repository"],
        int(job["pr_number"]),
        job["head_sha"],
    )
    finish_job(int(job["id"]), state, detail, check_id)
    print(
        f"JOB={job['id']} PR={job['pr_number']} SHA={job['head_sha'][:12]} "
        f"STATE={state} CHECK={check_id}",
        flush=True,
    )


def daemon() -> None:
    ensure_tables()
    bootstrap_cursor()
    print("DISTRIBUTED_POSTGRES_DAEMON=READY", flush=True)
    while True:
        try:
            enqueue_new_deliveries()
            job = claim_job()
            if job is None:
                time.sleep(1)
                continue
            run_job(job)
        except Exception as exc:
            print(f"DAEMON_ERROR={type(exc).__name__}:{exc}", flush=True)
            time.sleep(3)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--repo", default=TARGET_REPO, choices=[TARGET_REPO])
    run.add_argument("--pr", type=int, required=True)
    run.add_argument("--sha", required=True)
    sub.add_parser("daemon")
    args = parser.parse_args()

    ensure_tables()
    if args.command == "daemon":
        daemon()
        return
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.sha):
        raise SystemExit("invalid SHA")
    state, detail, check_id = execute_exact_sha(args.repo, args.pr, args.sha.lower())
    print(f"STATE={state}")
    print(f"CHECK_RUN_ID={check_id or ''}")
    print(detail)
    raise SystemExit(0 if state in {"PASS", "STALE"} else 1)


if __name__ == "__main__":
    main()