from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
import sqlite3
import subprocess
import time

import pytest

from test_distributed_shadow_checks import (
    POSTGRES_REPROFILE,
    POSTGRES_SCHEDULER,
    SCHEDULER,
    init_deliveries,
    load_publisher,
)


SHA = "a" * 40


class GitHub:
    def __init__(self):
        self.checks = {}
        self.creates = 0
        self.completions = 0
        self.current = True
        self.create_loss = None
        self.complete_loss = None

    def request(self, path, token):
        assert token == "test-token"
        if "/commits/" in path:
            return 200, {"total_count": len(self.checks), "check_runs": list(self.checks.values())}
        check = self.checks.get(int(path.rsplit("/", 1)[1]))
        return (200, check.copy()) if check else (404, {})

    def create(self, token, external_id):
        assert token == "test-token"
        self.creates += 1
        check_id = self.creates
        if self.create_loss != "before":
            self.checks[check_id] = {
                "id": check_id,
                "name": "distributed-python",
                "head_sha": SHA,
                "external_id": external_id,
                "app": {"id": 4982054},
                "status": "queued",
            }
        if self.create_loss:
            raise OSError("response lost")
        return check_id

    def complete(self, token, _id, conclusion, title, detail):
        assert token == "test-token"
        self.completions += 1
        if self.complete_loss != "before":
            self.checks[_id].update(
                status="completed",
                conclusion=conclusion,
                output={"title": title, "summary": detail},
            )
        if self.complete_loss:
            raise OSError("response lost")


@pytest.fixture
def staging(tmp_path):
    module = load_publisher()
    module.DB_PATH = tmp_path / "events.sqlite3"
    init_deliveries(module.DB_PATH)
    github = GitHub()
    module.request_json = github.request
    module.ensure_tables()
    now = [time.time()]
    manager = module.lifecycle()
    manager.clock = lambda: now[0]
    return module, github, now, manager


def enqueue(staging, pr=17, suite="python", *, created=None, state="PENDING"):
    module, _, now, manager = staging
    at = datetime.fromtimestamp(now[0] if created is None else created, timezone.utc).isoformat()
    with sqlite3.connect(module.DB_PATH) as db:
        return db.execute(
            f"""INSERT INTO {manager.table}
            (repository,pr_number,head_sha,suite,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)""",
            (module.TARGET_REPO, pr, SHA, suite, state, at, at),
        ).lastrowid


def read(staging, _id):
    module, _, _, manager = staging
    with sqlite3.connect(module.DB_PATH) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute(f"SELECT * FROM {manager.table} WHERE id=?", (_id,)).fetchone())


def drive(staging, result="PASS", *, execute=None):
    _, github, _, manager = staging
    [job] = manager.claim(1)

    def simulate(token, check_id, stage):
        if execute is not None:
            execute(job)
        if result == "WAITING_CAPACITY":
            return result, "no admitted worker", check_id
        conclusion = "success" if result == "PASS" else "failure"
        stage(conclusion, result, "verified result")
        return result, "verified result", check_id

    manager.run(
        job,
        name="distributed-python",
        authenticate=lambda: "test-token",
        current=lambda _: github.current,
        create=github.create,
        complete=github.complete,
        execute=simulate,
    )
    return job


def test_capacity_past_original_window_releases_executor_and_uses_same_check(staging):
    _, github, clock, manager = staging
    job_id = enqueue(staging)
    original = clock[0]
    for offset in (0, 5, 15, 35, 75, 135, 195, 255, 315):
        clock[0] = original + offset
        job = drive(staging, "WAITING_CAPACITY")
        assert job["id"] == job_id
        assert read(staging, job_id)["state"] == "PENDING"
        assert github.checks[1]["status"] == "queued"
        assert manager.claim(1) == []
    clock[0] += 65
    assert drive(staging)["state"] == "PASS"
    assert github.creates == github.completions == 1
    assert read(staging, job_id)["check_run_id"] == 1


@pytest.mark.parametrize(
    "result,expected", [("FAIL", "failure"), ("WAITING_CAPACITY", "action_required")]
)
def test_failure_is_distinct_from_capacity_deadline(staging, result, expected):
    _, github, clock, _ = staging
    job_id = enqueue(staging, created=clock[0] - 3601 if result == "WAITING_CAPACITY" else None)
    seen = []
    drive(staging, result, execute=seen.append)
    row = read(staging, job_id)
    assert github.checks[1]["conclusion"] == expected
    assert row["state"] == ("FAIL" if expected == "failure" else "INFRA_EXHAUSTED")
    assert len(seen) == (1 if expected == "failure" else 0)


def test_fair_claims_and_one_execution_per_pr(staging):
    _, _, _, manager = staging
    for suite in ("transport", "docker", "python"):
        enqueue(staging, 17, suite)
    enqueue(staging, 18)
    enqueue(staging, 19)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = [job for batch in pool.map(manager.claim, (3, 3)) for job in batch]
    assert [j["pr_number"] for j in claimed] == [17, 18, 19]
    assert claimed[0]["suite"] == "python"
    assert manager.claim(3) == []


def test_deferred_job_superseded_closes_only_its_exact_check(staging):
    _, github, clock, _ = staging
    job_id = enqueue(staging)
    drive(staging, "WAITING_CAPACITY")
    github.current = False
    clock[0] += 6
    drive(staging, execute=lambda _: pytest.fail("superseded job must never dispatch"))
    assert read(staging, job_id)["state"] == "STALE"
    assert github.checks[1]["conclusion"] == "cancelled"


@pytest.mark.parametrize("loss", ["before", "after"])
def test_create_response_loss_is_never_reposted(staging, loss):
    _, github, clock, _ = staging
    job_id = enqueue(staging)
    github.create_loss = loss
    drive(staging)
    if loss == "before":
        assert read(staging, job_id)["phase"] == "creating"
        clock[0] += 3601
        drive(staging, execute=lambda _: pytest.fail("no check identity"))
        assert read(staging, job_id)["state"] == "UNRECONCILED"
    else:
        assert read(staging, job_id)["state"] == "PASS"
    assert github.creates == 1


@pytest.mark.parametrize("loss", ["before", "after"])
def test_publish_response_loss_reconciles_without_reexecution(staging, loss):
    _, github, clock, manager = staging
    job_id = enqueue(staging)
    github.complete_loss = loss
    seen = []
    drive(staging, execute=seen.append)
    if loss == "before":
        assert read(staging, job_id)["state"] == "FINALIZING"
        github.complete_loss = None
        clock[0] += 6
        manager.recover(restart=True)
        drive(staging, execute=lambda _: pytest.fail("publication retry must not dispatch"))
    assert read(staging, job_id)["state"] == "PASS"
    assert len(seen) == 1 and github.creates == 1
    assert github.completions == (2 if loss == "before" else 1)


def test_persisted_result_remains_claimed_during_github_patch(staging, monkeypatch):
    _, github, _, manager = staging
    job_id = enqueue(staging)
    complete = github.complete

    def racing_claim(*args):
        assert read(staging, job_id)["publication"] is not None
        assert manager.claim(3) == []
        complete(*args)

    monkeypatch.setattr(github, "complete", racing_claim)
    assert drive(staging)["state"] == "PASS"
    assert github.completions == 1


def test_recovery_resumes_safe_claim_but_quarantines_expired_dispatch(staging):
    _, github, clock, manager = staging
    job_id = enqueue(staging)
    old_id = enqueue(staging, 123, state="RUNNING")
    [job] = manager.claim(1)
    manager.recover(restart=True)
    assert read(staging, job_id)["state"] == "PENDING"
    [job] = manager.claim(1)
    remote = manager.ensure_check(
        job, "distributed-python", "test-token", lambda x: github.create("test-token", x)
    )
    assert remote["id"] == 1
    manager.begin_dispatch(job)
    manager.recover(restart=True)
    assert manager.claim(1) == []
    clock[0] += 2101
    manager.recover(active=[job_id])
    assert read(staging, job_id)["state"] == "RUNNING"
    manager.recover()
    drive(staging, execute=lambda _: pytest.fail("ambiguous SSH dispatch must not repeat"))
    assert read(staging, job_id)["state"] == "UNRECONCILED"
    assert github.checks[1]["conclusion"] == "action_required"
    assert read(staging, old_id)["state"] == "RUNNING"


def test_main_priority_does_not_take_worker_or_queue_lease(staging):
    _, _, clock, manager = staging
    job_id = enqueue(staging)
    with manager.main_priority():
        drive(staging, execute=lambda _: pytest.fail("main priority blocks new candidate dispatch"))
    assert read(staging, job_id)["state"] == "PENDING"
    clock[0] += 6
    assert drive(staging)["state"] == "PASS"
    # The abandoned inode, with no owner, is safe for the next daemon.
    with manager.lock("daemon") as acquired:
        assert acquired
        with manager.lock("daemon") as duplicate:
            assert not duplicate
    with manager.lock("daemon") as recovered:
        assert recovered


@pytest.mark.parametrize(
    "suite, scheduler",
    [("python", SCHEDULER), ("postgres", POSTGRES_SCHEDULER), ("reprofile", POSTGRES_REPROFILE)],
)
@pytest.mark.parametrize("busy", ["before_probe", "after_probe"])
def test_real_scheduler_reports_pre_execution_worker_contention(tmp_path, suite, scheduler, busy):
    env = scheduler_environment(tmp_path, busy)
    args = [SHA] if suite == "reprofile" else [SHA, suite]
    result = subprocess.run(
        ["bash", str(scheduler), *args], env=env, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 75, result.stdout + result.stderr
    assert result.stdout.splitlines().count("CI_DISTRIBUTED_ADMISSION=WAITING_CAPACITY") == 1
    assert "CI_STATUS=PASS" not in result.stdout


def scheduler_environment(tmp_path, busy):
    registry = tmp_path / "registry.py"
    registry.write_text("""import sys
cmd=sys.argv[3]
if cmd in ('list-workers','field'):
    print('ci03')
elif cmd=='snapshot':
    print('{"hosts": []}')
""")
    capacity = tmp_path / "capacity.json"
    capacity.write_text(json.dumps({"pytest_full_seconds": {"ci03": 1}}))
    replan = tmp_path / "replan.py"
    replan.touch()
    ssh = tmp_path / "ssh"
    ssh.write_text("""#!/usr/bin/env bash
set -eu
command=${!#}
if [[ "$command" == 'test -x '* ]]; then exit 0; fi
if [[ "$command" == 'flock '* ]]; then
  if [[ "$TEST_BUSY" == before_probe ]]; then exit 1; fi
  if [[ "$TEST_BUSY" == kernel ]]; then
    if [[ -n "${TEST_PROBE_BARRIER:-}" ]]; then
      # Serialize the tiny probe critical sections in the harness, then
      # release both schedulers into the same real, nonblocking admission.
      flock -w 1 "$TEST_WORKER_LOCK" -c true
      touch "$TEST_PROBE_BARRIER/$$"
      for step in {1..100}; do
        count=$(find "$TEST_PROBE_BARRIER" -type f | wc -l)
        if (( count >= 2 )); then break; fi
        sleep .02
      done
      (( count >= 2 ))
    else
      flock -n "$TEST_WORKER_LOCK" -c true
    fi
  fi
  exit 0
fi
if [[ "$TEST_BUSY" == kernel ]]; then
  if flock -n "$TEST_WORKER_LOCK" -c 'echo CI_RUN_ADMITTED; sleep 1; echo CI_STATUS=PASS'; then exit 0; fi
  echo CI_WORKER_BUSY
  exit 75
fi
if [[ "$TEST_BUSY" == lost ]]; then exit 255; fi
if [[ "$TEST_BUSY" == test_failure ]]; then echo CI_STATUS=FAIL; exit 1; fi
echo CI_WORKER_BUSY
exit 75
""")
    ssh.chmod(0o755)
    return dict(
        os.environ,
        ANDY_CI_PERSISTENT_JOB="1",
        ANDY_CI_SSH_BIN=str(ssh),
        ANDY_CI_HOST_REGISTRY_CLI=str(registry),
        ANDY_CI_CAPACITY_FILE=str(capacity),
        ANDY_CI_HOST_REGISTRY_DB=str(tmp_path / "registry.sqlite3"),
        ANDY_CI_LOG_ROOT=str(tmp_path / "logs"),
        ANDY_CI_PROFILE_ROOT=str(tmp_path / "profiles"),
        ANDY_CI_REPLAN_POSTGRES=str(replan),
        TEST_BUSY=busy,
    )


@pytest.mark.parametrize(
    "mode,expected", [("lost", "WORKER_LOST"), ("test_failure", "TEST_FAILURE")]
)
def test_generic_scheduler_never_defers_a_started_or_ambiguous_execution(tmp_path, mode, expected):
    result = subprocess.run(
        ["bash", str(SCHEDULER), SHA, "python"],
        env=scheduler_environment(tmp_path, mode),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert "CI_DISTRIBUTED_ADMISSION=WAITING_CAPACITY" not in result.stdout
    [summary] = list((tmp_path / "logs").glob("*/summary.json"))
    assert json.loads(summary.read_text())["failure_class"] == expected


def test_two_real_probes_race_one_actual_worker_lock(tmp_path):
    barrier = tmp_path / "probes"
    barrier.mkdir()
    env = scheduler_environment(tmp_path, "kernel")
    env.update(TEST_WORKER_LOCK=str(tmp_path / "worker.lock"), TEST_PROBE_BARRIER=str(barrier))

    def run(lane):
        return subprocess.run(
            ["bash", str(SCHEDULER), SHA, lane], env=env, capture_output=True, text=True, timeout=15
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ("python", "transport")))
    assert sorted(result.returncode for result in results) == [0, 75]
    assert sum("CI_DISTRIBUTED_ADMISSION=WAITING_CAPACITY" in r.stdout for r in results) == 1
    logs = [path.read_text() for path in (tmp_path / "logs").glob("*/*ci03.log")]
    assert sum("CI_RUN_ADMITTED" in log for log in logs) == 1
    assert sum("CI_WORKER_BUSY" in log for log in logs) == 1


def test_priority_rotates_in_old_waiting_work(staging):
    _, _, clock, manager = staging
    manager.priority_prs = (455,)  # simulates an approved service configuration
    enqueue(staging, 123, created=clock[0] - 5)
    enqueue(staging, 124, created=clock[0] - 901)
    enqueue(staging, 455)
    assert [job["pr_number"] for job in manager.claim(3)] == [124, 455, 123]


@pytest.mark.parametrize("postgres", [False, True])
def test_publishers_run_nonblocking_capacity_contract_for_queued_jobs(monkeypatch, postgres):
    from test_distributed_shadow_checks import PUBLISHER

    module = (
        load_publisher(PUBLISHER.with_name("distributed_postgres_check.py"))
        if postgres
        else load_publisher()
    )

    class Impact:
        def required(self, suite):
            return True

    seen = []

    def run(command, *, persistent):
        seen.append((command, persistent))
        return subprocess.CompletedProcess(
            command, 75, "CI_DISTRIBUTED_ADMISSION=WAITING_CAPACITY\n"
        )

    monkeypatch.setattr(module, "get_pr_impact", lambda *_args: Impact())
    monkeypatch.setattr(module, "run_command", run)
    monkeypatch.setattr(
        module, "create_check", lambda *_args: pytest.fail("queue owns check identity")
    )
    args = (module.TARGET_REPO, 17, SHA) if postgres else (module.TARGET_REPO, 17, SHA, "python")
    state, _, check = module.execute_exact_sha(
        *args, queued=("synthetic", 42, lambda *_: pytest.fail("must stay pending"))
    )
    assert (state, check) == ("WAITING_CAPACITY", 42) and len(seen) == 1 and seen[0][1]
