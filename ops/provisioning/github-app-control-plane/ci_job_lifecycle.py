"""Durable admission and publication for the existing distributed CI queues."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
import sqlite3
import time

from merged_main_evidence import APP_ID

CAPACITY_DEADLINE_SECONDS = 3600
PUBLICATION_DEADLINE_SECONDS = 300
PRIORITY_AGING_SECONDS = 900
# Longer than the existing subprocess timeout. A vanished publisher cannot
# prove that an SSH dispatch ended, even after the local timeout.
EXECUTION_LEASE_SECONDS = 2100
TABLES = {"distributed_shadow_jobs", "distributed_postgres_jobs"}


def stamp():
    return datetime.now(timezone.utc).isoformat()


class Uncertain(RuntimeError):
    pass


class Lifecycle:
    def __init__(self, path, table, request, *, clock=None, priority_prs=None):
        if table not in TABLES:
            raise ValueError("unknown CI queue")
        self.path, self.table, self.request = path, table, request
        self.clock = clock or time.time
        raw = os.getenv("ANDY_CI_PRIORITY_PRS", "")
        configured = tuple(int(number) for number in raw.split(",")) if raw else ()
        self.priority_prs = configured if priority_prs is None else tuple(priority_prs)
        if len(self.priority_prs) > 16 or any(number <= 0 for number in self.priority_prs):
            raise ValueError("invalid configured CI priority PRs")

    def install(self, db):
        db.execute("BEGIN IMMEDIATE")
        current = {row[1] for row in db.execute(f"PRAGMA table_info({self.table})")}
        columns = {
            "not_before": "INTEGER NOT NULL DEFAULT 0",
            "retry_count": "INTEGER NOT NULL DEFAULT 0",
            "phase": "TEXT",
            "lease_until": "INTEGER",
            "publication": "TEXT",
        }
        for name, kind in columns.items():
            if name not in current:
                db.execute(f"ALTER TABLE {self.table} ADD COLUMN {name} {kind}")

    @contextmanager
    def lock(self, kind, *, shared=False):
        # Lock files, like the worker lock, are stable inodes. Never unlink.
        path = self.path.with_name(f"{self.path.name}.{self.table}.{kind}.lock")
        with path.open("a") as stream:
            try:
                kind = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                fcntl.flock(stream, kind | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True

    @contextmanager
    def main_priority(self):
        # Serialize announcements and give in-flight jobs the worker ownership
        # they already have. New candidate dispatch checks this lock.
        with self.lock("main") as acquired:
            if not acquired:
                raise RuntimeError("MAIN_CERTIFICATION_ALREADY_ACTIVE")
            yield

    def claim(self, limit):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            suite_order = (
                "CASE WHEN suite='python' THEN 0 ELSE 1 END,"
                if self.table == "distributed_shadow_jobs"
                else ""
            )
            priority = "0"
            if self.priority_prs:
                placeholders = ",".join("?" for _ in self.priority_prs)
                priority = f"job.repository='escossio/attention-router' AND job.pr_number IN ({placeholders})"
            old = datetime.fromtimestamp(
                self.clock() - PRIORITY_AGING_SECONDS, timezone.utc
            ).isoformat()
            # Prefer old ready work, while rotating among PRs instead of
            # filling the pool with the suites of a single PR.
            sql = f"""SELECT * FROM {self.table} AS job
                WHERE job.state IN ('PENDING','FINALIZING') AND job.not_before<=?
                AND NOT EXISTS (SELECT 1 FROM {self.table} AS active
                    WHERE active.repository=job.repository AND active.pr_number=job.pr_number
                    AND active.state='RUNNING' AND active.phase IS NOT NULL)
                ORDER BY CASE WHEN job.state='FINALIZING' THEN 0 WHEN job.created_at<=? THEN 1
                    WHEN {priority} THEN 2 ELSE 3 END,
                    job.created_at, {suite_order} job.id"""
            picked = []
            prs = set()
            for row in db.execute(sql, (int(self.clock()), old, *self.priority_prs)):
                pr = (row["repository"], row["pr_number"])
                if pr in prs:
                    continue
                prs.add(pr)
                picked.append(dict(row))
                if len(picked) == limit:
                    break
            for row in picked:
                self.update(
                    db,
                    row,
                    "RUNNING",
                    phase=row["phase"] or "claimed",
                    lease_until=int(self.clock()) + EXECUTION_LEASE_SECONDS,
                )
            return picked

    def update(self, db, job, state, **fields):
        fields.update(state=state, updated_at=stamp())
        assignments = ",".join(f"{field}=?" for field in fields)
        db.execute(
            f"UPDATE {self.table} SET {assignments} WHERE id=?", (*fields.values(), job["id"])
        )
        job.update(fields)

    def save(self, job, state, **fields):
        with sqlite3.connect(self.path) as db:
            self.update(db, job, state, **fields)

    def expired(self, job):
        return (
            self.clock()
            >= datetime.fromisoformat(job["created_at"]).timestamp() + CAPACITY_DEADLINE_SECONDS
        )

    def defer(self, job, detail, *, finalizing=False):
        count = job["retry_count"] + 1
        delay = min(60, 5 * 2 ** min(4, count - 1))
        self.save(
            job,
            "FINALIZING" if finalizing else "PENDING",
            not_before=int(self.clock()) + delay,
            retry_count=count,
            lease_until=None,
            detail=detail[:2000],
        )

    def recover(self, *, restart=False, active=()):
        """Release safe work after a singleton restart; quarantine ambiguous dispatches."""
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                f"SELECT * FROM {self.table} WHERE state='RUNNING' AND phase IS NOT NULL"
            ).fetchall()
            for row in rows:
                if row["id"] in active:
                    continue
                job = dict(row)
                phase = row["phase"]
                if restart and phase != "dispatch":
                    state = "FINALIZING" if row["publication"] else "PENDING"
                    self.update(db, job, state, not_before=0, lease_until=None)
                elif phase == "dispatch" and row["lease_until"] <= int(self.clock()):
                    if row["check_run_id"] is None:
                        self.update(
                            db,
                            job,
                            "UNRECONCILED",
                            detail="DISPATCH_LEASE_EXPIRED",
                            lease_until=None,
                        )
                    else:
                        result = {
                            "state": "UNRECONCILED",
                            "conclusion": "action_required",
                            "title": "CI dispatch lease expired",
                            "detail": "Verified terminal result unavailable.",
                            "deadline": int(self.clock()) + PUBLICATION_DEADLINE_SECONDS,
                        }
                        self.update(
                            db,
                            job,
                            "FINALIZING",
                            phase="publication",
                            publication=json.dumps(result),
                            not_before=0,
                            lease_until=None,
                        )

    def external_id(self, job):
        suite = job.get("suite", "postgres")
        return f"andy-ci:{job['pr_number']}:{job['head_sha']}:{suite}"

    def check_identity(self, job, name, remote):
        if not isinstance(remote, dict) or (
            remote.get("head_sha"),
            remote.get("name"),
            remote.get("external_id"),
            (remote.get("app") or {}).get("id"),
        ) != (job["head_sha"], name, self.external_id(job), APP_ID):
            raise Uncertain("CHECK_IDENTITY_MISMATCH")
        return remote

    def read_check(self, job, name, token):
        path = f"/repos/{job['repository']}/check-runs/{job['check_run_id']}"
        code, remote = self.request(path, token)
        if code != 200:
            raise Uncertain("CHECK_READ_UNAVAILABLE")
        remote = self.check_identity(job, name, remote)
        if remote.get("id") != job["check_run_id"]:
            raise Uncertain("CHECK_ID_MISMATCH")
        return remote

    def find_check(self, job, name, token):
        matches = []
        count = 0
        for page in range(1, 101):
            path = (
                f"/repos/{job['repository']}/commits/{job['head_sha']}/check-runs"
                f'?filter=all&per_page=100&page={page}'
            )
            code, body = self.request(path, token)
            if (
                code != 200
                or not isinstance(body, dict)
                or not isinstance(body.get("check_runs"), list)
            ):
                raise Uncertain("CHECK_LIST_UNAVAILABLE")
            if not isinstance(body.get("total_count"), int):
                raise Uncertain("CHECK_LIST_PARTIAL")
            rows = body["check_runs"]
            count += len(rows)
            for remote in rows:
                if (
                    remote.get("external_id") == self.external_id(job)
                    and (remote.get("app") or {}).get("id") == APP_ID
                ):
                    matches.append(self.check_identity(job, name, remote))
            if count >= body["total_count"]:
                break
            if len(rows) < 100:
                raise Uncertain("CHECK_LIST_PARTIAL")
        else:
            raise Uncertain("CHECK_LIST_EXHAUSTED")
        if len(matches) > 1:
            raise Uncertain("CHECK_DUPLICATE")
        return matches[0] if matches else None

    def ensure_check(self, job, name, token, create):
        if job["check_run_id"] is not None:
            return self.read_check(job, name, token)
        remote = self.find_check(job, name, token)
        if remote is None:
            # A lost creation response is not a license for another POST.
            if job["phase"] == "creating":
                raise Uncertain("CHECK_CREATE_UNRECONCILED")
            self.save(job, "RUNNING", phase="creating")
            try:
                check_id = create(self.external_id(job))
            except (OSError, RuntimeError, ValueError):
                remote = self.find_check(job, name, token)
                if remote is None:
                    raise Uncertain("CHECK_CREATE_UNRECONCILED") from None
            else:
                self.save(job, "RUNNING", phase="admitted", check_run_id=check_id)
                return {"status": "in_progress", "id": check_id}
        self.save(job, "RUNNING", phase="admitted", check_run_id=int(remote["id"]))
        return remote

    def begin_dispatch(self, job):
        self.save(
            job,
            "RUNNING",
            phase="dispatch",
            lease_until=int(self.clock()) + EXECUTION_LEASE_SECONDS,
        )

    def retain_capacity(self, job, detail):
        job["phase"] = "admitted"
        self.save(job, "RUNNING", phase="admitted")
        self.defer(job, detail)

    def stage_result(self, job, state, conclusion, title, detail):
        result = {
            "state": state,
            "conclusion": conclusion,
            "title": title,
            "detail": detail,
            "deadline": int(self.clock()) + PUBLICATION_DEADLINE_SECONDS,
        }
        self.save(
            job,
            "FINALIZING",
            phase="publication",
            publication=json.dumps(result),
            not_before=0,
            lease_until=None,
        )

    def publish(self, job, name, token, complete, current):
        result = json.loads(job["publication"])
        remote = self.read_check(job, name, token)
        if remote.get("status") != "completed":
            if result["state"] not in ("STALE", "UNRECONCILED") and not current(token):
                result.update(
                    state="STALE", conclusion="cancelled", title="CI candidate superseded"
                )
                self.save(job, "FINALIZING", publication=json.dumps(result))
            try:
                complete(
                    job["check_run_id"], result["conclusion"], result["title"], result["detail"]
                )
            except (OSError, RuntimeError, ValueError):
                remote = self.read_check(job, name, token)
                if remote.get("status") != "completed":
                    raise Uncertain("CHECK_COMPLETION_UNRECONCILED") from None
            else:
                self.save(
                    job,
                    result["state"],
                    detail=result["detail"][:2000],
                    publication=None,
                    phase=None,
                )
                return
        if remote.get("conclusion") != result["conclusion"]:
            raise Uncertain("CHECK_TERMINAL_CONTRADICTION")
        self.save(
            job, result["state"], detail=result["detail"][:2000], publication=None, phase=None
        )

    def run(self, job, *, name, authenticate, current, create, complete, execute):
        """Run a claimed row, preserving the external identity at every retry."""
        try:
            token = authenticate()
            if job["publication"]:
                self.publish(job, name, token, lambda *args: complete(token, *args), current)
                return
            if not current(token):
                if job["check_run_id"] is None and job["phase"] != "creating":
                    self.save(
                        job,
                        "STALE",
                        phase=None,
                        lease_until=None,
                        detail="PR closed or SHA superseded",
                    )
                    return
                if job["check_run_id"] is None:
                    remote = self.find_check(job, name, token)
                    if remote is None:
                        raise Uncertain("SUPERSEDED_CHECK_CREATION_UNRECONCILED")
                    self.save(job, "RUNNING", check_run_id=int(remote["id"]))
                self.stage_result(
                    job,
                    "STALE",
                    "cancelled",
                    "CI candidate superseded",
                    "PR closed or SHA superseded",
                )
            else:
                remote = self.ensure_check(
                    job, name, token, lambda external: create(token, external)
                )
                if remote.get("status") == "completed":
                    raise Uncertain("CHECK_COMPLETED_WITHOUT_LOCAL_RESULT")
                if self.expired(job):
                    self.stage_result(
                        job,
                        "INFRA_EXHAUSTED",
                        "action_required",
                        "CI capacity deadline exhausted",
                        "No verified run within the operational deadline.",
                    )
                else:
                    with self.lock("main", shared=True) as can_dispatch:
                        if not can_dispatch:
                            self.defer(job, "Integrated main certification has dispatch priority")
                            return
                        self.begin_dispatch(job)
                    # The worker owns the atomic flock. The main priority
                    # announcement blocks new admissions, never a running job.
                    publication = []
                    state, detail, _ = execute(
                        token, job["check_run_id"], lambda *args: publication.append(args)
                    )
                    if state == "WAITING_CAPACITY":
                        self.retain_capacity(job, detail)
                        return
                    if len(publication) != 1:
                        raise Uncertain("DISPATCH_RESULT_UNRECONCILED")
                    conclusion, title, detail = publication[0]
                    self.stage_result(job, state, conclusion, title, detail)
            self.publish(job, name, token, lambda *args: complete(token, *args), current)
        except (OSError, RuntimeError, ValueError) as error:
            detail = str(error) if isinstance(error, Uncertain) else type(error).__name__
            if job["phase"] == "dispatch" and job["check_run_id"] is not None:
                self.stage_result(
                    job, "UNRECONCILED", "action_required", "CI dispatch outcome uncertain", detail
                )
            elif (
                self.clock() >= json.loads(job["publication"])["deadline"]
                if job["publication"]
                else self.expired(job)
            ):
                self.save(job, "UNRECONCILED", detail=detail[:2000], lease_until=None)
            else:
                self.defer(job, detail, finalizing=bool(job["publication"]))


def serve(lifecycle, enqueue, run, parallel):
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

    with lifecycle.lock("daemon") as acquired:
        if not acquired:
            raise RuntimeError("CI_QUEUE_DAEMON_ALREADY_ACTIVE")
        lifecycle.recover(restart=True)
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            running = {}
            while True:
                try:
                    enqueue()
                    lifecycle.recover(active=running.values())
                    free = parallel - len(running)
                    if free:
                        for item in lifecycle.claim(free):
                            running[pool.submit(run, item)] = item["id"]
                    if not running:
                        time.sleep(1)
                        continue
                    done, _ = wait(running, timeout=1, return_when=FIRST_COMPLETED)
                    for future in done:
                        running.pop(future)
                        try:
                            future.result()
                        except (OSError, RuntimeError, sqlite3.Error, ValueError) as error:
                            print(f"CI_JOB_ERROR={type(error).__name__}", flush=True)
                except (OSError, RuntimeError, sqlite3.Error, ValueError) as error:
                    print(f"DAEMON_ERROR={type(error).__name__}", flush=True)
                    time.sleep(3)
