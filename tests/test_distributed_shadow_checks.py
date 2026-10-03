from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys
import types


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


def load_publisher():
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
            "distributed_shadow_checks_test", PUBLISHER
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
    assert "python, transport, docker" in invalid.stderr


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

    assert postgres.count("flock -n /srv/andy-ci/worker.lock") >= 2
    assert reprofile.count("flock -n /srv/andy-ci/worker.lock") >= 2

    assert 'ANDY_CI_CAPACITY_WAIT_SECONDS:-300' in generic
    assert "cannot lock ref" in generic
    assert "unable to update local ref" in generic
    assert "echo MIRROR_BUSY" in generic
    assert "WORKER_BUSY|MIRROR_BUSY" in generic


def test_all_distributed_scheduler_scripts_are_valid_bash():
    for script in (SCHEDULER, POSTGRES_SCHEDULER, POSTGRES_REPROFILE):
        result = subprocess.run(
            ["bash", "-n", str(script)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{script}: {result.stderr}"


def test_postgres_discovery_uses_correct_python_regex_and_deadline():
    postgres = POSTGRES_SCHEDULER.read_text()
    reprofile = POSTGRES_REPROFILE.read_text()
    expected = r"test_postgres.*\.py$"
    broken = r"test_postgres.*\\.py$"

    for content in (postgres, reprofile):
        assert expected in content
        assert broken not in content
        assert 'ANDY_CI_CAPACITY_WAIT_SECONDS:-300' in content

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
