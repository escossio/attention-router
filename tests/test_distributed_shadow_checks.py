from __future__ import annotations

import importlib.util
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import types

import pytest

from test_merged_main_evidence import HEAD, NEXT, GitHub


ROOT = Path(__file__).resolve().parents[1]
PUBLISHER = (
    ROOT
    / "ops/provisioning/github-app-control-plane/distributed_shadow_checks.py"
)
SCHEDULER = (
    ROOT
    / "ops/provisioning/distributed-ci-lab/control-plane/andy-ci-distributed-suite"
)
POSTGRES_SCHEDULER = (
    ROOT
    / "ops/provisioning/distributed-ci-lab/control-plane/andy-ci-distributed"
)
POSTGRES_REPROFILE = (
    ROOT
    / "ops/provisioning/distributed-ci-lab/control-plane/andy-ci-reprofile"
)


def load_publisher(path=PUBLISHER):
    fake_probe = types.ModuleType("probe")
    fake_probe.CONFIG = Path("/nonexistent/app.env")
    fake_probe.INSTALLATION_CONFIG = Path("/nonexistent/installation.env")
    fake_probe.load_env = lambda _path: {}
    fake_probe.make_jwt = lambda *_args, **_kwargs: "jwt"
    fake_probe.request_json = lambda *_args, **_kwargs: (500, {})
    previous = sys.modules.get("probe")
    sys.modules["probe"] = fake_probe
    try:
        spec = importlib.util.spec_from_file_location(
            "distributed_checks_test", path
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("probe", None)
        else:
            sys.modules["probe"] = previous


def init_deliveries(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE deliveries (
                delivery_id TEXT PRIMARY KEY,
                repository TEXT,
                action TEXT,
                pr_number INTEGER,
                head_sha TEXT
            )"""
        )
        db.commit()


@pytest.mark.parametrize("suite", ["postgres", "python"])
@pytest.mark.parametrize("case", [
    "valid", "main_advanced", "open", "unmerged", "wrong_number", "wrong_sha", "wrong_base", "wrong_repo",
    "diverged", "wrong_ancestor", "unreadable", "transport_error", "moved_during_run", "wrong_receipt", "worker_failure",
    "ordinary_pr", "closed_without_flag",
])
def test_manual_merged_main_cli_uses_exact_commit_and_real_execution(tmp_path, monkeypatch, suite, case):
    postgres = suite == "postgres"
    module = load_publisher(PUBLISHER.with_name("distributed_postgres_check.py") if postgres else PUBLISHER)
    sha, other = "a" * 40, "b" * 40
    api = GitHub(number=17)
    api.credentials(monkeypatch)
    api.pr.update(
        number=18 if case == "wrong_number" else 17,
        state="open" if case in {"open", "ordinary_pr"} else "closed",
        merged=case != "unmerged",
        merge_commit_sha=other if case == "wrong_sha" else sha,
    )
    if case in {"ordinary_pr", "closed_without_flag"}:
        api.pr["head"]["sha"] = sha
    if case == "wrong_repo":
        api.pr["base"]["repo"]["full_name"] = "other/project"
    if case == "wrong_base":
        api.pr["base"]["ref"] = "other"
    api.main = NEXT if case == "main_advanced" else sha
    comparisons = []

    def read_github(path, token, **kwargs):
        if case in {"ordinary_pr", "closed_without_flag"}:
            return 200, api.pr
        if "/compare/" in path:
            comparisons.append(path)
            if case == "unreadable":
                return 503, {}
            if case == "transport_error":
                raise OSError("connection unavailable")
            if case == "diverged" or (case == "moved_during_run" and len(comparisons) == 2):
                return 200, {"status": "diverged", "merge_base_commit": {"sha": sha}}
            if case == "wrong_ancestor":
                return 200, {"status": "identical", "merge_base_commit": {"sha": HEAD}}
        return api.transport(path, token, **kwargs)

    published, created, executed, impacts = [], [], [], []
    monkeypatch.setattr(module, "installation_token", lambda: "synthetic")
    monkeypatch.setattr(module, "request_json", read_github)
    monkeypatch.setattr(module, "create_check", lambda token, project, target, *args: created.append(target) or 42)
    monkeypatch.setattr(module, "complete_check", lambda token, project, run_id, state, *args: published.append(state))
    monkeypatch.setattr(module, "get_pr_impact", lambda *args: impacts.append(True) or types.SimpleNamespace(
        required=lambda _: False, summary=lambda _: "NOT_APPLICABLE"))
    ordinary = case in {"ordinary_pr", "closed_without_flag"}
    monkeypatch.setattr(module, "ensure_tables", lambda: None if ordinary else pytest.fail("manual run opened queue"))
    receipt_sha = other if case == "wrong_receipt" else sha
    digest = "d" * 64
    if postgres:
        summary = {"sha": receipt_sha, "schema_version": 5, "suite": suite, "status": "PASS",
                   "configured_workers": ["ci03"], "selection_sha256": digest, "profile_key": f"nodes-{digest}",
                   "selected_file_count": 3, "selected_test_count": 10, "total_passed_tests": 10,
                   "workers": {"ci03": {"status": "PASS", "rc": 0, "target_files": 3, "passed_tests": 10}}}
        command = [module.CI_RUN, sha, "postgres"]
    else:
        summary = {"sha": receipt_sha, "repository": "attention-router", "suite": suite, "status": "PASS",
                   "selected_worker": "ci02", "attempts": [
                       {"attempt": 1, "worker": "ci02", "classification": "PASS", "rc": 0, "duration_seconds": 1}]}
        command = [module.CI_RUN, "attention-router", sha, "python"]
    def run(argv):
        executed.append(argv)
        path = tmp_path / "summary.json"
        path.write_text(json.dumps(summary))
        return subprocess.CompletedProcess(argv, 1 if case == "worker_failure" else 0,
                                           f"CI_DISTRIBUTED_SUMMARY={path}\nCI_DISTRIBUTED_STATUS=PASS\n")
    monkeypatch.setattr(module, "run_command", run)
    args = ["publisher", "run", "--pr", "17", "--sha", sha]
    if not postgres:
        args.extend(["--suite", "python"])
    if not ordinary:
        args.append("--merged-main")
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(SystemExit) as stopped:
        module.main()
    ran = case in {"valid", "main_advanced", "moved_during_run", "wrong_receipt", "worker_failure"}
    assert executed == ([command] if ran else [])
    assert created == ([sha] if ran or case == "ordinary_pr" else [])
    assert impacts == ([True] if case == "ordinary_pr" else [])
    if case in {"valid", "main_advanced"}:
        assert published == ["success"] and len(comparisons) == 2 and stopped.value.code == 0
    elif ran:
        assert published == ["failure"] and stopped.value.code != 0
    elif ordinary:
        assert published == (["success"] if case == "ordinary_pr" else [])
    else:
        assert not published and stopped.value.code != 0


@pytest.mark.parametrize(("repo", "suite"), [
    ("escossio/attention-router", "transport"), ("escossio/attention-router", "docker"), ("escossio/andy-android", "android"),
])
def test_merged_main_cli_limits_shadow_publisher_to_canonical_python(monkeypatch, repo, suite):
    module = load_publisher()
    monkeypatch.setattr(module, "installation_token", lambda: pytest.fail("unexpected App access"))
    state, _, check_id = module.execute_exact_sha(repo, 17, "a" * 40, suite, merged_main=True)
    assert state == "REFUSED" and check_id is None


def test_shadow_publisher_enqueues_three_suites_per_exact_head(tmp_path):
    module = load_publisher()
    db_path = tmp_path / "events.sqlite3"
    module.DB_PATH = db_path
    init_deliveries(db_path)
    module.ensure_tables()
    module.bootstrap_cursor()

    sha = "a" * 40
    with sqlite3.connect(db_path) as db:
        db.execute(
            """INSERT INTO deliveries(
                delivery_id,repository,action,pr_number,head_sha
            ) VALUES(?,?,?,?,?)""",
            ("delivery-1", module.TARGET_REPO, "synchronize", 321, sha),
        )
        db.commit()

    assert module.enqueue_new_deliveries() == 3
    jobs = module.claim_jobs()
    assert {job["suite"] for job in jobs} == {"python", "transport", "docker"}
    assert {job["head_sha"] for job in jobs} == {sha}
    assert {job["state"] for job in jobs} == {"PENDING"}

    with sqlite3.connect(db_path) as db:
        states = {
            row[0]
            for row in db.execute(
                "SELECT state FROM distributed_shadow_jobs ORDER BY suite"
            )
        }
    assert states == {"RUNNING"}


def test_shadow_publisher_enqueues_android_suite(tmp_path):
    module = load_publisher()
    db_path = tmp_path / "events.sqlite3"
    module.DB_PATH = db_path
    init_deliveries(db_path)
    module.ensure_tables()
    module.bootstrap_cursor()

    sha = "d" * 40
    with sqlite3.connect(db_path) as db:
        db.execute(
            """INSERT INTO deliveries(
                delivery_id,repository,action,pr_number,head_sha
            ) VALUES(?,?,?,?,?)""",
            ("delivery-android", "escossio/andy-android", "synchronize", 78, sha),
        )
        db.commit()

    assert module.enqueue_new_deliveries() == 1
    jobs = module.claim_jobs()
    assert len(jobs) == 1
    assert jobs[0]["repository"] == "escossio/andy-android"
    assert jobs[0]["suite"] == "android"
    assert jobs[0]["head_sha"] == sha


def test_shadow_publisher_marks_pending_old_head_stale(tmp_path):
    module = load_publisher()
    db_path = tmp_path / "events.sqlite3"
    module.DB_PATH = db_path
    init_deliveries(db_path)
    module.ensure_tables()
    module.bootstrap_cursor()

    first = "b" * 40
    second = "c" * 40
    with sqlite3.connect(db_path) as db:
        db.execute(
            """INSERT INTO deliveries(
                delivery_id,repository,action,pr_number,head_sha
            ) VALUES(?,?,?,?,?)""",
            ("delivery-1", module.TARGET_REPO, "synchronize", 7, first),
        )
        db.commit()
    assert module.enqueue_new_deliveries() == 3

    with sqlite3.connect(db_path) as db:
        db.execute(
            """INSERT INTO deliveries(
                delivery_id,repository,action,pr_number,head_sha
            ) VALUES(?,?,?,?,?)""",
            ("delivery-2", module.TARGET_REPO, "synchronize", 7, second),
        )
        db.commit()
    assert module.enqueue_new_deliveries() == 3

    with sqlite3.connect(db_path) as db:
        old_states = {
            row[0]
            for row in db.execute(
                """SELECT state FROM distributed_shadow_jobs
                   WHERE head_sha=?""",
                (first,),
            )
        }
        new_states = {
            row[0]
            for row in db.execute(
                """SELECT state FROM distributed_shadow_jobs
                   WHERE head_sha=?""",
                (second,),
            )
        }
    assert old_states == {"STALE"}
    assert new_states == {"PENDING"}


def test_generic_scheduler_is_valid_bash_and_rejects_postgres():
    syntax = subprocess.run(
        ["bash", "-n", str(SCHEDULER)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr

    invalid = subprocess.run(
        ["bash", str(SCHEDULER), "0" * 40, "postgres"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert invalid.returncode == 64
    assert "invalid repository/suite pair" in invalid.stderr


def test_ci_service_units_allow_host_registry_state_writes():
    service_root = ROOT / "ops/provisioning/github-app-control-plane"
    for name in (
        "andy-github-distributed-postgres.service",
        "andy-github-distributed-shadow-ci.service",
    ):
        content = (service_root / name).read_text()
        line = next(
            item
            for item in content.splitlines()
            if item.startswith("ReadWritePaths=")
        )
        assert "/var/lib/andy-ci" in line.split("=")[1].split()


def test_distributed_schedulers_coordinate_worker_mirror_lock():
    generic = SCHEDULER.read_text()
    postgres = POSTGRES_SCHEDULER.read_text()
    reprofile = POSTGRES_REPROFILE.read_text()

    assert "flock -n /srv/andy-ci/worker.lock -c true" in generic
    assert "flock -n /srv/andy-ci/worker.lock -c true" in postgres
    assert "flock -n /srv/andy-ci/worker.lock -c true" in reprofile

    # Worker-owned discovery locks internally in andy-ci-run. Wrapping its
    # SSH invocation in another worker flock would deadlock discovery.
    worker = (
        ROOT / "ops/provisioning/distributed-ci-lab/worker/andy-ci-run"
    ).read_text()
    assert 'exec 9>"$BASE/worker.lock"' in worker
    assert "flock -n 9" in worker
    assert "ANDY_CI_DISCOVER_POSTGRES=1" in postgres
    assert "ANDY_CI_DISCOVER_POSTGRES=1" in reprofile
    assert '"flock -n /srv/andy-ci/worker.lock -c \'$remote_cmd\'"' not in postgres
    assert '"flock -n /srv/andy-ci/worker.lock -c \'$DISCOVERY_COMMAND\'"' not in reprofile

    assert 'ANDY_CI_CAPACITY_WAIT_SECONDS:-300' in generic
    assert "cannot lock ref" in generic
    assert "unable to update local ref" in generic
    assert "echo MIRROR_BUSY" in generic
    assert "WORKER_BUSY|MIRROR_BUSY" in generic


@pytest.mark.parametrize(
    "case", ["full", "partial", "excess", "duplicate", "stale", "empty", "sort_failure"]
)
def test_postgres_worker_verifies_full_manifest_before_natural_collection(tmp_path, monkeypatch, case):
    worker = (
        ROOT / "ops/provisioning/distributed-ci-lab/worker/andy-ci-run"
    ).read_text()
    guard = "verify_postgres_target_manifest() {\n"
    verifier = guard + worker.split(guard, 1)[1].split("\n}\n", 1)[0] + "\n}"
    sha = "a" * 40
    names = ["tests/integration/test_fixture.py", "tests/test_passive_vectors.py"]
    actual = tmp_path / "actual.txt"
    actual.write_text("\n".join(names) + "\n")
    manifest = tmp_path / "manifest.txt"
    headers = {
        "full": [f"sha={sha}", *reversed(names)],
        "partial": [f"sha={sha}", names[0]],
        "excess": [f"sha={sha}", *names, "tests/test_extra.py"],
        "duplicate": [f"sha={sha}", names[0], names[0]],
        "stale": ["sha=" + "b" * 40, *names],
        "empty": [f"sha={sha}"],
        "sort_failure": [f"sha={sha}", *names],
    }
    if case == "sort_failure":
        sort = tmp_path / "sort"
        sort.write_text("#!/bin/sh\nexit 1\n")
        sort.chmod(0o700)
        monkeypatch.setenv("PATH", str(tmp_path), prepend=os.pathsep)
    manifest.write_text("\n".join(headers[case]) + "\n")

    result = subprocess.run(
        ["bash", "-euc", verifier + '\nSHA="$1"; verify_postgres_target_manifest "$2" "$3"',
         "verify-postgres", sha, str(manifest), str(actual)],
        capture_output=True, text=True, check=False,
    )
    if case == "full":
        assert result.returncode == 0
        assert f"CI_POSTGRES_TARGET_MANIFEST_SHA={sha}" in result.stdout
        assert "CI_POSTGRES_FULL_COLLECTION_VERIFIED=2" in result.stdout
    else:
        assert result.returncode == 70
        assert "CI_POSTGRES_TARGET_MANIFEST_" in result.stderr

    scheduler = POSTGRES_SCHEDULER.read_text()
    assert """printf 'sha=%s\\n' "$SHA"; cat "$shard";""" in scheduler
    assert 'verify_postgres_target_manifest "$ANDY_CI_PYTEST_TARGETS_FILE" "$verified"' in worker
    assert 'PYTEST_ARGS+=("${PYTEST_TARGETS[@]}")' not in worker
    assert "PYTEST_ARGS=(-m postgres -q)" in worker


@pytest.mark.parametrize(
    "receipt", ["valid", "absent", "wrong_sha", "wrong_count", "wrong_nodes",
                "duplicate", "duplicate_passed", "conflicting_status", "no_execution"]
)
def test_postgres_coordinator_rejects_unverified_success(tmp_path, receipt):
    scheduler = POSTGRES_SCHEDULER.read_text()
    start = "classify_attempt() {\n"
    classifier = start + scheduler.split(start, 1)[1].split("\n}\n", 1)[0] + "\n}"
    sha = "a" * 40
    digest = "c" * 64
    verified_sha = "b" * 40 if receipt == "wrong_sha" else sha
    count = 68 if receipt == "wrong_count" else 69
    records = [f"CI_SHA={sha}", "CI_STATUS=PASS"]
    if receipt != "absent":
        records.extend([
            f"CI_POSTGRES_TARGET_MANIFEST_SHA={verified_sha}",
            f"CI_POSTGRES_FULL_COLLECTION_VERIFIED={count}",
            "CI_POSTGRES_FILE_COUNT=69",
            "CI_POSTGRES_TEST_COUNT=712",
            f"CI_POSTGRES_SELECTION_SHA256={digest if receipt != 'wrong_nodes' else 'd' * 64}",
        ])
    if receipt != "no_execution":
        records.append("CI_POSTGRES_PASSED_TEST_COUNT=712")
    if receipt == "duplicate_passed":
        records.append("CI_POSTGRES_PASSED_TEST_COUNT=712")
    if receipt == "duplicate":
        records.append("CI_POSTGRES_FULL_COLLECTION_VERIFIED=69")
    if receipt == "conflicting_status":
        records.append("CI_STATUS=FAIL")
    log = tmp_path / "worker.log"
    log.write_text("\n".join(records) + "\n")
    result = subprocess.run(
        ["bash", "-euc", classifier + '\nSHA="$1"; SELECTION_SHA="$3"; SELECTED_TESTS=712; '
         'passed="$(sed -n \'s/^CI_POSTGRES_PASSED_TEST_COUNT=//p\' "$2")"; '
         'classify_attempt 0 "$2" 69 "${passed:-0}"',
         "classify-postgres", sha, str(log), digest],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == ("PASS" if receipt == "valid" else "MANIFEST_MISMATCH")


def test_alembic_test_environment_is_scoped_to_disposable_worker_database():
    worker = (
        ROOT / "ops/provisioning/distributed-ci-lab/worker/andy-ci-run"
    ).read_text()
    postgres = worker.split("run_postgres() {", 1)[1].split("\n}\n", 1)[0]
    ready = 'docker exec "$PG_NAME" pg_isready'
    migration = 'APP_ENV=test DATABASE_URL="$disposable_url" python -m alembic upgrade head'
    assert postgres.index('docker run -d --name "$PG_NAME"') < postgres.index(ready)
    assert postgres.rindex(ready) < postgres.index(migration)
    assert 'local disposable_url="postgresql+psycopg://ar_test:' in postgres
    assert '127.0.0.1:${PG_PORT}/attention_router_test"' in postgres
    assert worker.count("APP_ENV=test") == 1


def test_all_distributed_scheduler_scripts_are_valid_bash():
    for script in (SCHEDULER, POSTGRES_SCHEDULER, POSTGRES_REPROFILE):
        result = subprocess.run(
            ["bash", "-n", str(script)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{script}: {result.stderr}"


def test_postgres_discovery_uses_exact_sha_pytest_markers_and_deadline():
    postgres = POSTGRES_SCHEDULER.read_text()
    reprofile = POSTGRES_REPROFILE.read_text()
    worker = (
        ROOT / "ops/provisioning/distributed-ci-lab/worker/andy-ci-run"
    ).read_text()
    collector = (ROOT / "scripts/ci_postgres_manifest.py").read_text()
    parser = (
        ROOT
        / "ops/provisioning/distributed-ci-lab/control-plane/parse-postgres-discovery.py"
    ).read_text()

    for content in (postgres, reprofile):
        assert "ANDY_CI_DISCOVER_POSTGRES=1" in content
        assert "parse-postgres-discovery.py" in content
        assert "grep '^tests/integration/test_postgres" not in content
        assert 'ANDY_CI_CAPACITY_WAIT_SECONDS:-300' in content

    assert 'python "$RUNNER_BIN/ci-postgres-manifest.py"' in worker
    assert '"--collect-only", "-m", "postgres"' in collector
    assert 'CI_POSTGRES_DISCOVERY_SHA_MISMATCH' in parser
    assert 'CI_POSTGRES_DISCOVERY_INCOMPLETE' in parser
    assert "local attempts=0" not in postgres
    assert "while (( attempts < 8 ))" not in postgres
    assert "local deadline=$(( $(date +%s) + CAPACITY_WAIT_SECONDS ))" in postgres
    assert "DISCOVERY_DEADLINE=$(( $(date +%s) + CAPACITY_WAIT_SECONDS ))" in reprofile


def test_postgres_scheduler_is_exclusive_to_ci03_by_default():
    postgres = POSTGRES_SCHEDULER.read_text()
    reprofile = POSTGRES_REPROFILE.read_text()

    for content in (postgres, reprofile):
        assert 'ANDY_CI_POSTGRES_WORKER:-ci03' in content
        assert "POSTGRES_CAPABLE_WORKERS" in content
        assert '[[ "$worker" == "$POSTGRES_WORKER" ]]' in content
        assert 'CONFIGURED_WORKERS+=("$worker")' in content

    assert "CI_DISTRIBUTED_POSTGRES_WORKER=$POSTGRES_WORKER" in postgres
    assert "POSTGRES_WORKER_NOT_CONFIGURED" in postgres
    assert "CI_REPROFILE_POSTGRES_WORKER=$POSTGRES_WORKER" in reprofile
    assert "POSTGRES_WORKER_NOT_CONFIGURED" in reprofile


def test_postgres_check_ui_reports_exclusive_worker_policy():
    publisher = PUBLISHER.parent / "distributed_postgres_check.py"
    content = publisher.read_text()
    assert "PostgreSQL worker policy: CI03" in content
    assert "CI01/CI02/CI03" not in content


@pytest.mark.parametrize("case", ["valid", "wrong_sha", "wrong_suite", "old_summary",
                                  "wrong_worker", "zero_tests", "wrong_files", "duplicate_summary"])
def test_postgres_publisher_requires_current_execution_summary(tmp_path, case):
    module = load_publisher(PUBLISHER.with_name("distributed_postgres_check.py"))
    sha, digest = "a" * 40, "c" * 64
    summary = {
        "sha": sha, "schema_version": 5, "suite": "postgres", "status": "PASS", "configured_workers": ["ci03"],
        "selection_sha256": digest, "profile_key": f"nodes-{digest}", "selected_file_count": 69,
        "selected_test_count": 712, "total_passed_tests": 712,
        "workers": {"ci03": {"status": "PASS", "rc": 0, "target_files": 69, "passed_tests": 712}},
    }
    notes = {
        "wrong_sha": {"sha": "b" * 40},
        "wrong_suite": {"suite": "python"},
        "old_summary": {"schema_version": 3},
        "wrong_worker": {"configured_workers": ["ci02"]},
        "zero_tests": {"total_passed_tests": 0},
        "wrong_files": {"selected_file_count": 68},
    }
    summary.update(notes.get(case, {}))
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(summary))
    log = f"CI_DISTRIBUTED_SUMMARY={path}\nCI_DISTRIBUTED_STATUS=PASS\n"
    if case == "duplicate_summary":
        log += f"CI_DISTRIBUTED_SUMMARY={path}\n"
    assert module.verified_success(module.extract_summary(log), sha, log) is (case == "valid")
    if case == "valid":
        waiting = "CI_DISTRIBUTED_STATUS=WAITING_FOR_DISCOVERY_CAPACITY\n"
        assert module.verified_success(summary, sha, waiting + log)
        assert not module.verified_success(summary, sha, "CI_DISTRIBUTED_STATUS=FAIL\n" + log)


@pytest.mark.parametrize("mode", ["retry_busy", "stale_profile", "missing_duration"])
def test_reprofile_binds_streamed_durations_to_exact_selection(tmp_path, monkeypatch, mode):
    sha = "a" * 40
    nodeid = "tests/test_passive_vectors.py::test_synthetic"
    digest = sha256(json.dumps([nodeid], separators=(",", ":")).encode()).hexdigest()
    control = POSTGRES_REPROFILE.parent
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("plan-postgres.py", "parse-postgres-discovery.py"):
        (bin_dir / name).symlink_to(control / name)
    (tmp_path / "capacity.json").write_text('{"pytest_full_seconds":{"ci03":1}}')
    (tmp_path / "registry.py").write_text(
        'import sys\ncommand=sys.argv[3]\n'
        'if command == "list-workers": print("ci03")\n'
        'if command == "field": print("synthetic-worker")\n',
    )
    discovery = (
        f"CI_SHA={sha}\nCI_SUITE=postgres\nCI_POSTGRES_DISCOVERY_SHA={sha}\n"
        f"CI_POSTGRES_FILE=tests/test_passive_vectors.py\nCI_POSTGRES_FILE_COUNT=1\n"
        f"CI_POSTGRES_TEST_COUNT=1\nCI_POSTGRES_SELECTION_SHA256={digest}\nCI_STATUS=PASS\n"
    )
    profile = discovery.replace(f"CI_POSTGRES_DISCOVERY_SHA={sha}\n", "").replace(
        "CI_STATUS=PASS", "CI_POSTGRES_PASSED_TEST_COUNT=1\nCI_STATUS=PASS",
    )
    if mode == "stale_profile":
        profile = profile.replace(f"CI_SHA={sha}", f"CI_SHA={'b' * 40}")
    duration_node = nodeid if mode != "missing_duration" else nodeid.replace("synthetic", "missing")
    (tmp_path / "discovery").write_text(discovery)
    (tmp_path / "profile").write_text(f"0.11s call {duration_node}\n{profile}")
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/bin/bash\ncase \"${*: -1}\" in\n"
        '  ANDY_CI_DISCOVER_POSTGRES=*) cat "$FIXTURE/discovery";;\n'
        '  ANDY_CI_PROFILE_DURATIONS=*)\n'
        '    if [[ "$MODE" == retry_busy && ! -f "$FIXTURE/retried" ]]; then\n'
        '      touch "$FIXTURE/retried"; echo CI_WORKER_BUSY; exit 75\n'
        '    fi\n'
        '    cat "$FIXTURE/profile";;\n'
        "esac\n",
    )
    ssh.chmod(0o700)
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o700)
    for key, value in {
        "ANDY_CI_BASE": tmp_path, "ANDY_CI_LOG_ROOT": tmp_path / "logs",
        "ANDY_CI_HOST_REGISTRY_CLI": tmp_path / "registry.py",
        "ANDY_CI_CAPACITY_FILE": tmp_path / "capacity.json",
        "ANDY_CI_PROFILE_ROOT": tmp_path / "profiles", "ANDY_CI_SSH_BIN": ssh,
        "FIXTURE": tmp_path, "MODE": mode,
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv("PATH", str(bin_dir), prepend=os.pathsep)
    result = subprocess.run(
        ["bash", str(POSTGRES_REPROFILE), sha], capture_output=True, text=True,
        check=False, timeout=10,
    )
    published = tmp_path / "profiles" / f"nodes-{digest}" / "manifest.json"
    if mode == "retry_busy":
        assert result.returncode == 0, result.stdout + result.stderr
        assert (tmp_path / "retried").exists()
        assert json.loads(published.read_text())["source_sha"] == sha
    else:
        assert result.returncode != 0
        assert not published.exists()



def test_generic_scheduler_and_worker_support_android_repository():
    scheduler = SCHEDULER.read_text()
    worker_path = (
        ROOT
        / "ops/provisioning/distributed-ci-lab/worker/andy-ci-run"
    )
    worker = worker_path.read_text()

    worker_syntax = subprocess.run(
        ["bash", "-n", str(worker_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert worker_syntax.returncode == 0, worker_syntax.stderr
    assert "andy-android:android" in scheduler
    assert "andy-ci-run '$REPOSITORY' '$SHA' '$SUITE'" in scheduler
    assert "https://github.com/escossio/andy-android.git" in worker
    assert "andy-android-ci:api37-jdk17-v2" in worker
    assert "ANDROID_USER_HOME=/tmp/andy-android-user" in worker
    assert "JAVA_TOOL_OPTIONS=-Duser.home=/tmp/andy-home" in worker
    assert 'mkdir -p "$HOME" "$ANDROID_USER_HOME"' in worker
    assert ":sdk:client-api:test" in worker
    assert ":app:assembleDebug" in worker


def test_android_toolchain_is_pinned_to_jdk17_and_existing_sdk_contract():
    dockerfile = (
        ROOT
        / "ops/provisioning/distributed-ci-lab/worker/android-ci.Dockerfile"
    ).read_text()
    assert dockerfile.startswith("FROM eclipse-temurin:17-jdk-jammy")
    assert "COPY --chown=1000:1000 . /opt/android-sdk" in dockerfile
    assert "JAVA_TOOL_OPTIONS=-Duser.home=/tmp/andy-home" in dockerfile
    assert "platforms/android-37.0" in dockerfile
    assert "build-tools/36.0.0" in dockerfile
