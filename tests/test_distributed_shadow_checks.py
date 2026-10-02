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
