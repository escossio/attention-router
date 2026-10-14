"""Synthetic contracts for the read-only GitHub CI queue audit."""

import hashlib
import importlib.util
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "ops/provisioning/github-app-control-plane/queue_audit.py"
SPEC = importlib.util.spec_from_file_location("queue_audit", SOURCE)
assert SPEC and SPEC.loader
audit_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_module)


def fixture_db(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE deliveries(delivery_id TEXT);
            CREATE TABLE distributed_postgres_jobs(
              id INTEGER, repository TEXT, pr_number INTEGER, head_sha TEXT,
              state TEXT, check_run_id INTEGER, created_at TEXT, updated_at TEXT);
            CREATE TABLE distributed_shadow_jobs(
              id INTEGER, repository TEXT, pr_number INTEGER, head_sha TEXT,
              suite TEXT, state TEXT, check_run_id INTEGER, created_at TEXT,
              updated_at TEXT);
            CREATE TABLE distributed_postgres_meta(key TEXT,value TEXT);
            CREATE TABLE distributed_shadow_meta(key TEXT,value TEXT);
            INSERT INTO deliveries VALUES ('synthetic-1');
            INSERT INTO distributed_postgres_meta VALUES ('delivery_cursor','1');
            INSERT INTO distributed_shadow_meta VALUES ('delivery_cursor','1');
            """
        )


def test_crashed_job_is_reported_without_mutation(tmp_path):
    path = tmp_path / "events.sqlite3"
    fixture_db(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO distributed_postgres_jobs VALUES "
            "(1,'owner/repo',4,?,'RUNNING',NULL,'t1','t1')",
            ("a" * 40,),
        )
        db.execute(
            "INSERT INTO distributed_shadow_jobs VALUES "
            "(2,'owner/repo',4,?,'transport','PENDING',NULL,'t2','t2')",
            ("b" * 40,),
        )
    before = hashlib.sha256(path.read_bytes()).digest()
    report = audit_module.audit(path)
    assert report["needs_review"] is True
    assert report["queues"]["distributed_postgres_jobs"]["unresolved"][0]["check_run_id"] is None
    assert report["queues"]["distributed_shadow_jobs"]["unresolved"][0]["head_sha"] == "b" * 40
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_cursor_lag_is_reported(tmp_path):
    path = tmp_path / "events.sqlite3"
    fixture_db(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE distributed_shadow_meta SET value='0'")
    report = audit_module.audit(path)
    assert report["needs_review"] is True
    assert report["queues"]["distributed_shadow_jobs"]["delivery_cursor"] == 0


def test_missing_schema_fails_closed(tmp_path):
    path = tmp_path / "events.sqlite3"
    sqlite3.connect(path).close()
    with pytest.raises(ValueError, match="missing required schema"):
        audit_module.audit(path)
