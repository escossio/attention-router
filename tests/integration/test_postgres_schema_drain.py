"""Exercise the canonical G3 runner and a real disposable PostgreSQL transaction.

The remote authorization and archive receipt are synthetic fixtures only.
All actual migrations, catalog/ACL comparison, private attestation checks,
PostgreSQL writer preflight and maintenance hold are the canonical code.
"""
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import runpy
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from attention_router.application.context_schema_release import approved_context_schema_release
from attention_router.config import settings
from attention_router.infrastructure.maintenance_gate import ENV, GateError, MaintenanceGate

pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[2]
BASE = "0067_vector_lock_order"
HEAD = "0072_context_index_fence"


def migrate(conn, action, target):
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = conn
    config.set_main_option("script_location", str(ROOT / "alembic"))
    with approved_context_schema_release(conn):
        getattr(command, action)(config, target)


def private_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def test_schema_runner_hold_spans_real_commit_rollback_and_fault(Session, tmp_path, monkeypatch):
    engine = Session.kw["bind"]
    url = engine.url
    # Hard refusal if this does not belong to the disposable CI fixture.
    assert url.drivername == "postgresql+psycopg" and url.username == "ar_test"
    assert url.host in ("127.0.0.1", "localhost", "postgres")
    assert re.fullmatch(r"attention_router_test_[0-9a-f]{10}", url.database or "")
    assert os.environ.get("PUBLIC_POSTGRES_TEST_URL")
    assert make_url(os.environ["PUBLIC_POSTGRES_TEST_URL"]).set(database=url.database) == url
    release = runpy.run_path(str(ROOT / "ops/context-canary/release_schema.py"))
    audit = runpy.run_path(str(ROOT / "ops/context-canary/audit_live_readonly.py"))
    role_plan = runpy.run_path(str(ROOT / "ops/context-canary/role_plan.py"))
    review = runpy.run_path(str(ROOT / "ops/context-canary/preflight_review.py"))
    run = release["run"]
    role = "fixture_drain_" + uuid4().hex[:12]
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setenv("ANDY_SCHEMA_RELEASE_EXPLICIT", "SCHEMA_ONLY_NO_TENANT")
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    catalog_path = tmp_path / "catalog.json"
    acl_path = tmp_path / "acl.json"
    archive = tmp_path / "synthetic-backup-receipt-fixture.bin"
    archive.write_bytes(b"synthetic fixture, not a database backup")
    archive.chmod(0o600)
    stamp = datetime.now(UTC).timestamp() - 20
    os.utime(archive, (stamp, stamp))
    archive_sha = release["digest"](archive)
    record = {}
    active_gate = None
    active_binding = None
    transitions = []

    def fake_github(root, approval_path, comment_id, operation):
        assert root == ROOT and comment_id == 17 and record["operation"] == operation
        assert release["schema_approval_ref"](json.loads(approval_path.read_text())) == record["approval_ref"]
        return dict(record)

    def observe_lock(phase):
        if active_gate is None:
            return
        assert active_gate.status()["execution_state"] == "STARTED"
        with pytest.raises(GateError, match="GATE_EXECUTION_BUSY"):
            active_gate.release_maintenance(active_binding["window"], active_binding["hold"], reconciled=True)
        with active_gate.admission("worker") as admitted:
            assert not admitted
        transitions.append(phase)

    def checked_engine(raw_url, **kwargs):
        assert make_url(raw_url) == url
        result = create_engine(raw_url, **kwargs)
        event.listen(result, "commit", lambda conn: observe_lock("commit"))
        event.listen(result, "rollback", lambda conn: observe_lock("rollback"))
        return result

    monkeypatch.setitem(run.__globals__, "verify_github_release", fake_github)
    monkeypatch.setitem(run.__globals__, "create_engine", checked_engine)

    def revision():
        with engine.connect() as conn:
            return conn.scalar(text("SELECT version_num FROM alembic_version"))

    def setup(operation):
        nonlocal active_gate, active_binding
        roster = sorted(release["SCHEMA_REQUIRED_PARTICIPANTS"])
        gate = MaintenanceGate.initialize(tmp_path / ("gate-" + uuid4().hex), roster)
        for participant in roster:
            gate.observe(participant)
        state = gate.request_drain()
        hold = gate.claim_maintenance(state["window"])
        binding = {"gate_id": state["gate_id"], "window": state["window"], "hold": hold,
                   "participants": roster}
        now = datetime.now(UTC)
        approval = {
            "version": 1, "operation": operation, "confirmation": release["CONFIRM"][operation],
            "approved_at_utc": now.isoformat(), "database": info["database"], "system_id": info["system_id"],
            "source_sha256": release["source_digest"](ROOT),
            "catalog_sha256": release["digest"](catalog_path), "acl_sha256": release["digest"](acl_path),
            "backup_path": str(archive), "backup_sha256": archive_sha,
            "restore_status": "RESTORE_VERIFIED", "restore_backup_sha256": archive_sha,
            "restore_verified_at_utc": (now - timedelta(seconds=3)).isoformat(),
            "maintenance_gate": binding,
        }
        approval["approval_ref"] = release["schema_approval_ref"](approval)
        path = tmp_path / ("synthetic-approval-" + uuid4().hex + ".json")
        private_json(path, approval)
        record.update(version=2, operation=operation, gate="G3", approval_ref=approval["approval_ref"],
                      expires_at=(now + timedelta(minutes=10)).isoformat())
        monkeypatch.setenv(ENV, str(gate.directory))
        active_gate, active_binding = gate, binding
        transitions.clear()
        return path

    def invoke(action, path):
        return run(action, root=ROOT, acl_path=acl_path, catalog_path=catalog_path,
                   approval_path=path, github_comment_id=17 if path else None)

    def reconcile(expected_state):
        nonlocal active_gate, active_binding
        assert active_gate.status()["execution_state"] == expected_state
        assert active_gate.status()["state"] == "DRAINED"
        with pytest.raises(GateError, match="GATE_RECONCILIATION_REQUIRED"):
            active_gate.release_maintenance(active_binding["window"], active_binding["hold"])
        active_gate.release_maintenance(active_binding["window"], active_binding["hold"], reconciled=True)
        active_gate.resume(active_binding["window"])
        active_gate = active_binding = None

    try:
        with engine.begin() as conn:
            migrate(conn, "downgrade", BASE)
            conn.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS")
            conn.exec_driver_sql(f"GRANT SELECT ON public.resources TO {role}")
            conn.exec_driver_sql("""
                INSERT INTO google_contacts_connections
                (id,tenant_id,owner_id,status,envelope,generation,next_run,failures)
                VALUES ('drain-fixture-contact','fixture-tenant','fixture-owner','ACTIVE','{}','a',0,0)
            """)
        with engine.connect() as conn, conn.begin():
            conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            info = release["baseline"](conn)
            catalog = audit["snapshot"](conn)
            acl = role_plan["audit_catalog"](conn, legacy_roles=sorted(catalog["effective_access_roles"]))
            assert review["evaluate"](catalog, acl, root=ROOT)["status"] == "PASS_FOR_DISPOSABLE_REHEARSAL"
        private_json(catalog_path, catalog)
        private_json(acl_path, acl)

        # Read-only check is deliberately independent of maintenance admission.
        monkeypatch.delenv(ENV, raising=False)
        assert invoke("check", None)["status"] == "CHECK_ONLY_NO_DDL"
        assert revision() == BASE

        # The real upgrade executes, then a controlled exception must roll back
        # the entire PostgreSQL transaction while the hold remains exclusive.
        path = setup("schema_apply")
        upgrade = command.upgrade

        def fault_after_upgrade(config, target):
            observe_lock("ddl")
            upgrade(config, target)
            raise RuntimeError("synthetic failure after real DDL")

        with monkeypatch.context() as fault:
            fault.setattr(command, "upgrade", fault_after_upgrade)
            with pytest.raises(RuntimeError, match="synthetic failure after real DDL"):
                invoke("apply", path)
        assert "ddl" in transitions and "rollback" in transitions
        assert revision() == BASE
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT to_regclass('public.andy_context_fenced_tenants') IS NULL"))
        reconcile("UNCONFIRMED")

        # New authorized synthetic window: actual canonical apply and commit.
        path = setup("schema_apply")
        result = invoke("apply", path)
        assert result == {"status": "SCHEMA_RELEASE_COMMITTED", "revision": HEAD}
        assert "commit" in transitions and revision() == HEAD
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM public.andy_context_fenced_tenants")) == 0
            assert conn.scalar(text("SELECT count(*) FROM public.context_consent_scopes")) == 0
            assert conn.scalar(text("SELECT count(*) FROM public.context_source_consents")) == 0
        with pytest.raises(RuntimeError, match="GATE_EXECUTION_ALREADY_ATTEMPTED"):
            invoke("apply", path)
        reconcile("COMPLETED")

        # Rollback has a distinct operation, hold and approval; Contacts survive.
        path = setup("schema_rollback")
        result = invoke("rollback", path)
        assert result == {"status": "SCHEMA_RELEASE_COMMITTED", "revision": BASE}
        assert "commit" in transitions and revision() == BASE
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT id FROM google_contacts_connections")) == "drain-fixture-contact"
        reconcile("COMPLETED")
    finally:
        # Restore the disposable fixture's normal head for the remaining tests.
        # These actions are on the test database verified above, never the AGT.
        with engine.begin() as conn:
            if conn.scalar(text("SELECT version_num FROM alembic_version")) == BASE:
                migrate(conn, "upgrade", HEAD)
            if conn.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": role}):
                conn.exec_driver_sql(f"DROP OWNED BY {role}")
                conn.exec_driver_sql(f"DROP ROLE {role}")
