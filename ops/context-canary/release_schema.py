"""Privileged, explicitly approved schema-only release runner for Andy context.

Default action is READ-ONLY check. --apply and --rollback require an operator-
owned, short-lived approval file bound to target database, instance, backup,
catalog, ACL and code digest. No tenant enrollment, consent grant, route mount,
role creation, or service restart can happen in this runner.

This prevents accidental schema promotion, NOT attacks by an existing DBA.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from importlib.util import find_spec
import json
import os
from pathlib import Path
import runpy
from time import monotonic
import re
import sys
from stat import S_ISREG

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from attention_router.application.context_schema_release import (
    approved_context_schema_release,
)
from attention_router.config import settings


CONFIRM = {
    "schema_apply": "I APPROVE SCHEMA APPLY ONLY",
    "schema_rollback": "I APPROVE SCHEMA ROLLBACK ONLY",
    "roles_apply": "I APPROVE ROLE APPLY ONLY",
    "roles_rollback": "I APPROVE ROLE ROLLBACK ONLY",
}
FILES = (
    "attention_router/config.py",
    "alembic/env.py",
    "alembic/versions/0068_google_contacts_cache.py",
    "alembic/versions/0069_cognitive_revocation_v0.py",
    "alembic/versions/0070_context_integrity.py",
    "alembic/versions/0071_context_source_consent.py",
    "alembic/versions/0072_context_index_fence.py",
    "attention_router/application/context_schema_release.py",
    "attention_router/application/context_index_fence.py",
    "attention_router/infrastructure/vector_attachment_provenance.py",
    "ops/context-canary/release_schema.py",
    "ops/context-canary/verify_github_promotion.py",
    "ops/context-canary/release_roles.py",
    "ops/context-canary/audit_live_readonly.py",
    "ops/context-canary/preflight_review.py",
    "ops/context-canary/role_plan.py",
)


def digest(path: Path) -> str:
    block_hash = sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            block_hash.update(block)
    return block_hash.hexdigest()


def source_digest(root: Path) -> str:
    h = sha256()
    for name in FILES:
        f = root / name
        h.update(name.encode())
        h.update(bytes.fromhex(digest(f)))
    return h.hexdigest()


def private_file(path: Path) -> None:
    st = path.lstat()
    if not S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise ValueError("RELEASE_PRIVATE_FILE_OWNER_OR_MODE")


def system_id(conn) -> str:
    return str(conn.execute(text(
        "SELECT system_identifier::text FROM pg_control_system()"
    )).scalar_one())


def baseline(conn) -> dict:
    revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    enrolled = conn.execute(text(
        "SELECT count(*) FROM pg_catalog.pg_class "
        "WHERE relnamespace='public'::regnamespace "
        "AND relname='andy_context_fenced_tenants'"
    )).scalar_one()
    return {
        "database": conn.execute(text("SELECT current_database()")).scalar_one(),
        "system_id": system_id(conn),
        "revision": revision,
        "fence_table_count": enrolled,
    }


def _load_review(root: Path):
    audit = runpy.run_path(str(root / "ops/context-canary/audit_live_readonly.py"))
    acl_module = runpy.run_path(str(root / "ops/context-canary/role_plan.py"))
    review = runpy.run_path(str(root / "ops/context-canary/preflight_review.py"))
    return audit["snapshot"], acl_module["audit_catalog"], review["evaluate"]


def verify_snapshot(conn, *, root: Path, acl_path: Path, catalog_path: Path, revision="0067_vector_lock_order"):
    private_file(acl_path)
    private_file(catalog_path)
    stored_acl = json.loads(acl_path.read_text(encoding="utf-8"))
    stored_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    snapshot_fn, acl_fn, evaluator = _load_review(root)

    current_acl = acl_fn(conn, legacy_roles=stored_acl["roles"])
    current_catalog = snapshot_fn(conn)
    # Session counts and long-transaction counts are point-in-time
    # observations. Only stable ACL/schema metadata is compared exactly.
    stable_fields = (
        "schema_revision", "contact_columns", "contact_indexes",
        "vector_policies", "rls_tables", "roles", "memberships",
        "effective_access_roles", "extensions", "column_acls", "shared_policies",
    )
    if (
        current_acl != stored_acl
        or any(field not in stored_catalog or current_catalog.get(field) != stored_catalog[field]
               for field in stable_fields)
    ):
        raise RuntimeError("RELEASE_LIVE_CATALOG_DRIFT")
    report = evaluator(current_catalog, current_acl, root=root, revision=revision)
    if report["blockers"] or report["status"] != "PASS_FOR_DISPOSABLE_REHEARSAL":
        raise RuntimeError("RELEASE_PREFLIGHT_DENIED")
    return report


def verify_approval(path: Path, *, operation: str, root: Path,
                    snapshot_path: Path, acl_path: Path, conn) -> dict:
    private_file(path)
    approval = json.loads(path.read_text(encoding="utf-8"))
    if approval.get("version") != 1 or approval.get("confirmation") != CONFIRM[operation]:
        raise RuntimeError("RELEASE_OPERATOR_APPROVAL_MISSING")
    if approval.get("operation") != operation:
        raise RuntimeError("RELEASE_OPERATION_MISMATCH")
    try:
        issued = datetime.fromisoformat(approval["approved_at_utc"])
        if issued.tzinfo is None:
            raise ValueError("naive")
        age = (datetime.now(timezone.utc) - issued.astimezone(timezone.utc)).total_seconds()
        if not (0 <= age <= 1800):
            raise ValueError("expired")
    except (KeyError, ValueError, TypeError) as error:
        raise RuntimeError("RELEASE_APPROVAL_EXPIRED") from error
    info = baseline(conn)
    if (
        approval.get("database") != info["database"]
        or approval.get("system_id") != info["system_id"]
        or approval.get("source_sha256") != source_digest(root)
        or approval.get("catalog_sha256") != digest(snapshot_path)
        or approval.get("acl_sha256") != digest(acl_path)
    ):
        raise RuntimeError("RELEASE_ATTESTATION_MISMATCH")
    backup_path = Path(approval.get("backup_path", ""))
    if (
        not backup_path.is_absolute() or not backup_path.exists()
        or not backup_path.is_file() or backup_path.stat().st_size == 0
    ):
        raise RuntimeError("RELEASE_BACKUP_REQUIRED")
    private_file(backup_path)
    if (
        not isinstance(approval.get("backup_sha256"), str)
        or digest(backup_path) != approval["backup_sha256"]
    ):
        raise RuntimeError("RELEASE_BACKUP_DIGEST_MISMATCH")
    # Require a separately confirmed restoration of exactly this archive,
    # not merely a backup filename with a matching digest.
    if (
        approval.get("restore_status") != "RESTORE_VERIFIED"
        or approval.get("restore_backup_sha256") != approval["backup_sha256"]
    ):
        raise RuntimeError("RELEASE_RESTORE_EVIDENCE_REQUIRED")
    try:
        restored_at = datetime.fromisoformat(approval["restore_verified_at_utc"])
        if restored_at.tzinfo is None:
            raise ValueError("naive")
        age_after_restore = (issued - restored_at).total_seconds()
        if not (0 <= age_after_restore <= 1800):
            raise ValueError("expired")
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("RELEASE_RESTORE_EVIDENCE_EXPIRED") from error
    if restored_at.timestamp() < backup_path.stat().st_mtime:
        raise RuntimeError("RELEASE_RESTORE_PREDATES_BACKUP")
    # A valid old backup is not a valid pre-release backup. An operator must
    # capture and restore-check a fresh image close to the approved window.
    age_at_approval = issued.timestamp() - backup_path.stat().st_mtime
    if not (0 <= age_at_approval <= 1800):
        raise RuntimeError("RELEASE_BACKUP_OUTSIDE_APPROVED_WINDOW")
    return approval


def _preflight_running_writers(conn):
    # A brief maintenance window must be prepared before calling --apply.
    # Stop rather than block production write transactions for an unknown time.
    active = conn.execute(text("""
        SELECT count(*) FROM pg_stat_activity
        WHERE datname=current_database() AND pid<>pg_backend_pid()
          AND (state='active' OR xact_start IS NOT NULL)
    """)).scalar_one()
    if active:
        raise RuntimeError("RELEASE_ACTIVE_DATABASE_WRITERS")
    if not conn.execute(text(
        "SELECT pg_try_advisory_xact_lock(1095648346, 2097152)"
    )).scalar_one():
        raise RuntimeError("RELEASE_MAINTENANCE_LOCK_BUSY")


def verify_github_release(root: Path, approval_path: Path | None,
                          comment_id: int | None, operation: str) -> dict:
    if not comment_id or approval_path is None:
        raise RuntimeError("RELEASE_GITHUB_APPROVAL_REQUIRED")
    if root.resolve() != Path(__file__).resolve().parents[2]:
        raise RuntimeError("RELEASE_RUNNER_CHECKOUT_MISMATCH")
    application = find_spec("attention_router")
    if application is None or application.origin is None or Path(application.origin).resolve() != (
        root / "attention_router/__init__.py"
    ).resolve():
        raise RuntimeError("RELEASE_APPLICATION_CHECKOUT_MISMATCH")
    private_file(approval_path)
    approval_ref = json.loads(approval_path.read_text(encoding="utf-8")).get("approval_ref")
    if not isinstance(approval_ref, str) or not approval_ref:
        raise RuntimeError("RELEASE_APPROVAL_REF_REQUIRED")
    gate = runpy.run_path(str(root / "ops/context-canary/verify_github_promotion.py"))
    return gate["verify"](comment_id, operation, root, source_digest(root), approval_ref=approval_ref,
                          return_record=True)


def check_window(record, approval):
    now = datetime.now(timezone.utc)
    if record["approval_ref"] != approval.get("approval_ref"):
        raise RuntimeError("RELEASE_APPROVAL_REF_CHANGED")
    approved = datetime.fromisoformat(approval["approved_at_utc"])
    if now >= datetime.fromisoformat(record["expires_at"]) or (now - approved).total_seconds() > 1800:
        raise RuntimeError("RELEASE_APPROVAL_EXPIRED")


def report_failure(error):
    # Driver exceptions may include bound identifiers, DDL or connection
    # diagnostics. Keep CLI output restricted to our own symbolic refusals.
    message = str(error) if type(error) in (RuntimeError, ValueError) else ""
    if not re.fullmatch(r"(?:RELEASE|ROLES_RELEASE|CANARY|CONTEXT)_[A-Z0-9_]+", message):
        message = "RELEASE_FAILED"
    print(message, file=sys.stderr)
    return 1


def run(action: str, *, root: Path, acl_path: Path,
        catalog_path: Path, approval_path: Path | None, github_comment_id: int | None = None) -> dict:
    if settings.app_env != "production":
        raise RuntimeError("RELEASE_REQUIRES_PRODUCTION_CONFIGURATION")
    if action not in ("check", "apply", "rollback"):
        raise ValueError("RELEASE_ACTION_UNKNOWN")
    if (
        action != "check"
        and os.environ.get("ANDY_SCHEMA_RELEASE_EXPLICIT") != "SCHEMA_ONLY_NO_TENANT"
    ):
        raise RuntimeError("RELEASE_OPERATOR_WINDOW_NOT_OPEN")
    if action != "check":
        record = verify_github_release(root, approval_path, github_comment_id, "schema_" + action)
    # Never accept a DSN on the command line or log credentials.
    raw_url = os.environ["DATABASE_URL"]
    engine = create_engine(raw_url, hide_parameters=True, connect_args={"connect_timeout": 4})
    try:
        with engine.connect() as conn:
            with conn.begin():
                if action == "check":
                    conn.execute(text("SET TRANSACTION READ ONLY"))
                conn.execute(text("SET LOCAL idle_in_transaction_session_timeout='60s'"))
                conn.execute(text("SET LOCAL statement_timeout='20000ms'"))
                conn.execute(text("SET LOCAL lock_timeout='1500ms'"))
                info = baseline(conn)
                wanted = "0072_context_index_fence" if action == "rollback" else "0067_vector_lock_order"
                if info["revision"] != wanted:
                    raise RuntimeError("RELEASE_BASELINE_REVISION_MISMATCH")
                if action != "rollback" and info["fence_table_count"]:
                    raise RuntimeError("RELEASE_FENCE_ALREADY_PRESENT")

                if action in ("check", "apply"):
                    report = verify_snapshot(
                        conn, root=root, acl_path=acl_path, catalog_path=catalog_path,
                    )
                else:
                    report = {"status": "ROLLBACK_REQUIRES_ABSENCE_OF_CANARY_HISTORY"}
                    if conn.execute(text(
                        "SELECT count(*) FROM andy_context_fenced_tenants WHERE enabled"
                    )).scalar_one():
                        raise RuntimeError("RELEASE_ROLLBACK_TENANT_STILL_ENROLLED")
                    if conn.execute(text("SELECT count(*) FROM context_consent_scopes")).scalar_one():
                        raise RuntimeError("RELEASE_ROLLBACK_CONSENT_HISTORY_EXISTS")
                    if conn.execute(text("SELECT count(*) FROM context_source_consents")).scalar_one():
                        raise RuntimeError("RELEASE_ROLLBACK_CONSENT_HISTORY_EXISTS")

                if action == "check":
                    return {
                        "status": "CHECK_ONLY_NO_DDL",
                        "approved_for_production": False,
                        "preflight": report["status"],
                        "revision": info["revision"],
                    }

                approval = verify_approval(
                    approval_path, operation="schema_" + action, root=root,
                    snapshot_path=catalog_path, acl_path=acl_path, conn=conn,
                )
                check_window(record, approval)
                started = monotonic()
                _preflight_running_writers(conn)
                cfg = Config(str(root / "alembic.ini"))
                cfg.attributes["connection"] = conn
                cfg.set_main_option("script_location", str(root / "alembic"))
                # Invariant: the callback is held only for this validated
                # connection, within the already-open migration transaction.
                with approved_context_schema_release(conn):
                    if action == "apply":
                        command.upgrade(cfg, "0072_context_index_fence")
                    else:
                        command.downgrade(cfg, "0067_vector_lock_order")
                expected = "0072_context_index_fence" if action == "apply" else "0067_vector_lock_order"
                if baseline(conn)["revision"] != expected:
                    raise RuntimeError("RELEASE_MIGRATION_RESULT_UNEXPECTED")
                if action == "apply":
                    if conn.scalar(text("""
                        SELECT EXISTS (SELECT 1 FROM public.andy_context_fenced_tenants
                                      UNION ALL SELECT 1 FROM public.context_consent_scopes)
                    """)):
                        raise RuntimeError("RELEASE_UNEXPECTED_AUTHORITY")
                else:
                    verify_snapshot(conn, root=root, acl_path=acl_path, catalog_path=catalog_path)
                check_window(record, approval)
                if monotonic() - started > 45:
                    raise RuntimeError("RELEASE_DDL_WINDOW_EXCEEDED")
                return {"status": "SCHEMA_RELEASE_COMMITTED", "revision": expected}
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("check", "apply", "rollback"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--acl", type=Path, required=True)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--github-comment-id", type=int)
    args = parser.parse_args()
    result = run(
        args.action, root=args.root.resolve(), acl_path=args.acl,
        catalog_path=args.catalog, approval_path=args.approval, github_comment_id=args.github_comment_id,
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(report_failure(error)) from None
