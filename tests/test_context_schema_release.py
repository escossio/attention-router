"""Production schema migration authorization is connection-local and fail-closed."""

from pathlib import Path
import runpy

import pytest
from sqlalchemy import create_engine

from attention_router.application.context_schema_release import (
    approved_context_schema_release,
    context_schema_release_authorized,
)
from attention_router.config import settings


ROOT = Path(__file__).resolve().parents[1]
release = runpy.run_path(str(ROOT / "ops/context-canary/release_schema.py"))


def test_migration_authorization_only_within_exact_transaction():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.connect() as conn:
            assert not context_schema_release_authorized(conn)
            with conn.begin():
                assert not context_schema_release_authorized(conn)
                with approved_context_schema_release(conn):
                    assert context_schema_release_authorized(conn)
                    with engine.connect() as other:
                        assert not context_schema_release_authorized(other)
                assert not context_schema_release_authorized(conn)
            assert not context_schema_release_authorized(conn)
        with pytest.raises(RuntimeError, match="TRANSACTION_REQUIRED"):
            with engine.connect() as conn, approved_context_schema_release(conn):
                pass
    finally:
        engine.dispose()


def test_release_is_disabled_without_production_environment(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "test")
    with pytest.raises(RuntimeError, match="PRODUCTION_CONFIGURATION"):
        release["run"](
            "check",
            root=ROOT, acl_path=ROOT / "missing-acl.json",
            catalog_path=ROOT / "missing-catalog.json",
            approval_path=None,
        )


def test_source_digest_is_stable_and_includes_guard_and_runner():
    files = release["FILES"]
    assert "attention_router/config.py" in files
    assert "alembic/versions/0072_context_index_fence.py" in files
    assert "ops/context-canary/release_schema.py" in files
    assert "attention_router/application/context_schema_release.py" in files
    first = release["source_digest"](ROOT)
    assert len(first) == 64
    assert first == release["source_digest"](ROOT)


def test_symlink_and_insecure_file_are_not_accepted(tmp_path):
    target = tmp_path / "approval.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o644)
    with pytest.raises(ValueError, match="OWNER_OR_MODE"):
        release["private_file"](target)
    target.chmod(0o600)
    release["private_file"](target)
    link = tmp_path / "alias.json"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="OWNER_OR_MODE"):
        release["private_file"](link)


def test_new_transaction_on_same_connection_does_not_inherit_approval():
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        conn.begin()
        with approved_context_schema_release(conn):
            conn.rollback()
            conn.begin()
            assert not context_schema_release_authorized(conn)
        conn.rollback()
    engine.dispose()


def test_backup_restore_hash_and_order_are_required(monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta
    import json
    import os

    now = datetime.now(UTC)
    backup = tmp_path / "synthetic.bin"
    backup.write_bytes(b"synthetic approval fixture")
    backup.chmod(0o600)
    stamp = now.timestamp() - 60
    os.utime(backup, (stamp, stamp))
    archive_hash = release["digest"](backup)
    approval = tmp_path / "approval.json"
    inputs = dict(
        version=1, operation="schema_apply", confirmation=release["CONFIRM"]["schema_apply"],
        database="synthetic", system_id="synthetic", source_sha256="code",
        approved_at_utc=now.isoformat(), catalog_sha256=archive_hash,
        acl_sha256=archive_hash, backup_path=str(backup), backup_sha256=archive_hash,
        restore_status="RESTORE_VERIFIED", restore_backup_sha256=archive_hash,
        restore_verified_at_utc=(now - timedelta(seconds=10)).isoformat(),
    )
    check = release["verify_approval"]
    monkeypatch.setitem(check.__globals__, "baseline", lambda _: {"database": "synthetic", "system_id": "synthetic"})
    monkeypatch.setitem(check.__globals__, "source_digest", lambda _: "code")

    def verify(**overrides):
        approval.write_text(json.dumps(dict(inputs, **overrides)), encoding="utf-8")
        approval.chmod(0o600)
        return check(approval, operation="schema_apply", root=ROOT, snapshot_path=backup, acl_path=backup, conn=None)

    assert verify()["restore_backup_sha256"] == archive_hash
    with pytest.raises(RuntimeError, match="RESTORE_EVIDENCE_REQUIRED"):
        verify(restore_backup_sha256="different")
    with pytest.raises(RuntimeError, match="RESTORE_PREDATES_BACKUP"):
        verify(restore_verified_at_utc=(now - timedelta(minutes=2)).isoformat())
    with pytest.raises(RuntimeError, match="OPERATION_MISMATCH"):
        verify(operation="roles_apply")
