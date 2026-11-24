#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

sys.path.insert(0, "/usr/local/lib/andy-github-app")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe import CONFIG, INSTALLATION_CONFIG, load_env, make_jwt, request_json
from ci_impact_policy import get_pr_impact
from ci_job_lifecycle import Lifecycle, serve
from merged_main_evidence import (
    APP_ID, EvidenceError, PublicationUncertain, authenticate, finalize, validate,
)

DB_PATH = Path("/var/lib/andy-github-app/events.sqlite3")
QUEUE_TABLE = "distributed_postgres_jobs"
CHECK_NAME = "distributed-postgres"
TARGET_REPO = "escossio/attention-router"
CI_RUN = "/usr/local/lib/andy-ci/bin/andy-ci-distributed"
CI_REPROFILE = "/usr/local/lib/andy-ci/bin/andy-ci-reprofile"
ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
DEBOUNCE_SECONDS = 5
def lifecycle() -> Lifecycle:
    return Lifecycle(DB_PATH, QUEUE_TABLE, request_json)


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
        lifecycle().install(db)
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
                       (delivery_id,repository,pr_number,head_sha,state,created_at,updated_at,not_before)
                       VALUES (?,?,?,?, 'PENDING', ?,?,?)""",
                    (delivery_id, repo, pr_number, sha.lower(), stamp, stamp, int(time.time()) + DEBOUNCE_SECONDS),
                )
                created += cur.rowcount
                if cur.rowcount:
                    db.execute(
                        """UPDATE distributed_postgres_jobs
                           SET state='STALE', detail='superseded before execution', updated_at=?
                           WHERE repository=? AND pr_number=? AND state='PENDING' AND check_run_id IS NULL AND phase IS NULL
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
    jobs = lifecycle().claim(1)
    return jobs[0] if jobs else None


def current_pr(token: str, repo: str, pr_number: int) -> dict:
    status, pr = request_json(f"/repos/{repo}/pulls/{pr_number}", token)
    if status != 200:
        raise RuntimeError(f"PR_LOOKUP_FAILED status={status}")
    return pr


def create_check(token: str, repo: str, sha: str, pr_number: int, *, external_id: str | None = None) -> int:
    status, check = request_json(
        f"/repos/{repo}/check-runs",
        token,
        method="POST",
        body={
            "name": CHECK_NAME,
            "head_sha": sha,
            **({"external_id": external_id} if external_id is not None else {}),
            **({"status": "queued"} if external_id is not None else {"status": "in_progress", "started_at": now_iso()}),
            "output": {
                "title": "Distributed PostgreSQL gate running",
                "summary": f"PR #{pr_number} exact SHA {sha} PostgreSQL worker policy: CI03",
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
def run_command(argv: list[str], *, persistent: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=1800,
        check=False,
        env=dict(os.environ, ANDY_CI_PERSISTENT_JOB="1") if persistent else None,
    )


def extract_summary(output: str) -> dict | None:
    matches = re.findall(r"^CI_DISTRIBUTED_SUMMARY=(.+)$", output, re.M)
    if len(matches) != 1:
        return None
    path = Path(matches[0].strip())
    try:
        summary = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return summary if isinstance(summary, dict) else None


def verified_success(summary: dict | None, sha: str, output: str) -> bool:
    terminal = re.findall(r"^CI_DISTRIBUTED_STATUS=(PASS|FAIL)$", output, re.M)
    if summary is None or terminal != ["PASS"]:
        return False
    if (summary.get("schema_version") != 5 or summary.get("sha") != sha
            or summary.get("suite") != "postgres" or summary.get("status") != "PASS"
            or summary.get("configured_workers") != ["ci03"]):
        return False
    digest = summary.get("selection_sha256")
    files = summary.get("selected_file_count")
    tests = summary.get("selected_test_count")
    passed = summary.get("total_passed_tests")
    if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or summary.get("profile_key") != f"nodes-{digest}"
            or any(type(count) is not int for count in (files, tests, passed))
            or not 0 < files <= tests or not 0 < passed <= tests):
        return False
    workers = summary.get("workers")
    if not isinstance(workers, dict) or set(workers) != {"ci03"}:
        return False
    worker = workers["ci03"]
    return (
        isinstance(worker, dict) and worker.get("status") == "PASS" and worker.get("rc") == 0
        and worker.get("target_files") == files and worker.get("passed_tests") == passed
    )


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
    if selection := summary.get("selection_sha256"):
        parts[3:3] = [
            f"Selected tests: {summary.get('selected_test_count')}",
            f"Selected files: {summary.get('selected_file_count')}",
            f"Selection SHA-256: {selection}",
        ]
    for name in ("ci01", "ci02", "ci03"):
        data = workers.get(name) or {}
        parts.append(
            f"| {name.upper()} | {data.get('status','?')} | "
            f"{data.get('duration_seconds','?')} s | "
            f"{data.get('passed_tests','?')} | {data.get('target_files','?')} |"
        )
    return "\n".join(parts)
def execute_exact_sha(repo: str, pr_number: int, sha: str, *, merged_main: bool = False, queued=None) -> tuple[str, str, int | None]:
    if queued is not None:
        token, check_id, stage = queued
    elif merged_main:
        try:
            app = authenticate(request_json)
            integration = validate(app, repo, pr_number, sha)
        except EvidenceError as error:
            return "REFUSED", str(error), None
        token = app.token
    else:
        token = installation_token()
        pr = current_pr(token, repo, pr_number)
        current_sha = ((pr.get("head") or {}).get("sha") or "").lower()
        if pr.get("state") != "open" or current_sha != sha.lower():
            return "STALE", "PR is closed or head SHA moved before execution.", None

    request = partial(request_json, token=token)
    if queued is None:
        try:
            check_id = create_check(token, repo, sha, pr_number)
        except (OSError, RuntimeError, ValueError):
            if not merged_main:
                raise
            return "UNRECONCILED", "MERGED_MAIN_CHECK_CREATION_UNRECONCILED", None

    def finish(conclusion, title, detail):
        if queued is not None:
            stage(conclusion, title, detail)
            return

        def publish():
            complete_check(token, repo, check_id, conclusion, title, detail)

        if merged_main:
            finalize(app, check_id, sha, CHECK_NAME, conclusion, publish)
        else:
            publish()
    reprofiled = False

    def schedule(command):
        return run_command(command) if queued is None else run_command(command, persistent=True)

    def deferred(result):
        return queued is not None and result.returncode == 75 and "CI_DISTRIBUTED_ADMISSION=WAITING_CAPACITY" in result.stdout.splitlines()

    try:
        if not merged_main:
            impact = get_pr_impact(repo, pr_number, sha, request)
            if not impact.required("postgres"):
                detail = impact.summary("postgres")
                finish("success", "NOT APPLICABLE: distributed-postgres (no test run)", detail)
                return "NOT_APPLICABLE", detail, check_id
        result = schedule([CI_RUN, sha, "postgres"])
        if deferred(result):
            return "WAITING_CAPACITY", "CI03 unavailable before PostgreSQL execution", check_id
        if result.returncode == 66:
            reprofiled = True
            repro = schedule([CI_REPROFILE, sha])
            if deferred(repro):
                return "WAITING_CAPACITY", "CI03 unavailable before reprofile execution", check_id
            if repro.returncode != 0:
                detail = "Automatic reprofile failed.\n\n" + repro.stdout[-12000:]
                if queued is not None and "CI_REPROFILE_STATUS=TEST_FAILURE" not in repro.stdout.splitlines():
                    finish("action_required", "Distributed PostgreSQL reprofile infrastructure failed", detail)
                    return "INFRA_EXHAUSTED", detail, check_id
                finish("failure", "Distributed PostgreSQL reprofile failed", detail)
                return "FAIL", detail, check_id
            result = schedule([CI_RUN, sha, "postgres"])
            if deferred(result):
                return "WAITING_CAPACITY", "CI03 unavailable before PostgreSQL execution", check_id

        summary = extract_summary(result.stdout)
        if result.returncode == 0 and verified_success(summary, sha, result.stdout):
            if merged_main and integration != validate(app, repo, pr_number, sha):
                raise EvidenceError("CONTRADICTION", "integration_changed_during_run")
            text = render_summary(summary, reprofiled=reprofiled)
            finish("success", "Distributed PostgreSQL gate passed", text,
            )
            return "PASS", text, check_id

        detail = render_summary(summary, reprofiled=reprofiled)
        if result.returncode == 0:
            detail = "PostgreSQL execution summary did not verify.\n\n" + detail
        detail += "\n\n" + result.stdout[-12000:]
        if queued is not None and not any(worker.get("status") == "TEST_FAILURE" for worker in (summary or {}).get("workers", {}).values()):
            finish("action_required", "Distributed PostgreSQL infrastructure failed", detail)
            return "INFRA_EXHAUSTED", detail, check_id
        finish("failure", "Distributed PostgreSQL gate failed", detail,
        )
        return "FAIL", detail, check_id
    except PublicationUncertain as error:
        return "UNRECONCILED", str(error), check_id
    except Exception as exc:
        if queued is not None:
            raise RuntimeError("CI_DISPATCH_UNRECONCILED") from None
        detail = f"{type(exc).__name__}: {exc}"
        try:
            finish("failure", "Distributed PostgreSQL gate error", detail,
            )
        except PublicationUncertain as error:
            return "UNRECONCILED", str(error), check_id
        except Exception:
            return "FAIL", detail, check_id
        return "FAIL", detail, check_id
def run_job(job: dict) -> None:
    repo, number, sha = job["repository"], int(job["pr_number"]), job["head_sha"]
    name = CHECK_NAME

    def current(token):
        pr = current_pr(token, repo, number)
        return pr.get("state") == "open" and ((pr.get("head") or {}).get("sha") or "").lower() == sha

    lifecycle().run(
        job, name=name, authenticate=installation_token, current=current,
        create=lambda token, external: create_check(token, repo, number, sha, external_id=external),
        complete=lambda token, *result: complete_check(token, repo, *result),
        execute=lambda token, check, stage: execute_exact_sha(repo, number, sha, queued=(token, check, stage)),
    )
    print(f"JOB={job['id']} PR={number} SHA={sha[:12]} STATE={job['state']} CHECK={job['check_run_id']}", flush=True)


def daemon() -> None:
    ensure_tables()
    bootstrap_cursor()
    print("DISTRIBUTED_POSTGRES_DAEMON=READY", flush=True)
    serve(lifecycle(), enqueue_new_deliveries, run_job, 1)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--repo", default=TARGET_REPO, choices=[TARGET_REPO])
    run.add_argument("--pr", type=int, required=True)
    run.add_argument("--sha", required=True)
    run.add_argument("--merged-main", action="store_true", help="Certify the integrated commit of a merged main PR.")
    run.add_argument("--preflight", action="store_true", help="Validate merged main and App authority without publishing or dispatching.")
    sub.add_parser("daemon")
    args = parser.parse_args()

    if args.command == "daemon":
        daemon()
        return
    if args.preflight:
        if not args.merged_main:
            parser.error("--preflight requires the canonical --merged-main target")
        try:
            result = validate(authenticate(request_json), args.repo, args.pr, args.sha.lower())
        except EvidenceError as error:
            print(f"PREFLIGHT=REFUSED {error}")
            raise SystemExit(1) from None
        print(f"PREFLIGHT=PASS app={APP_ID} pr={result.number} head={result.head} "
              f"commit={result.commit} tree={result.tree} strategy={result.strategy} head_checks={len(result.checks)}")
        return
    if not args.merged_main:
        ensure_tables()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.sha):
        raise SystemExit("invalid SHA")
    priority = lifecycle().main_priority() if args.merged_main and DB_PATH.is_file() else nullcontext()
    with priority:
        state, detail, check_id = execute_exact_sha(args.repo, args.pr, args.sha.lower(), merged_main=args.merged_main)
    print(f"STATE={state}")
    print(f"CHECK_RUN_ID={check_id or ''}")
    print(detail)
    raise SystemExit(0 if state in {"PASS", "STALE"} else 1)


if __name__ == "__main__":
    main()
