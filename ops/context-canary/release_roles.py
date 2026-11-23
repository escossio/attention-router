"""Explicit, schema-0072-only role/RLS cutover; NOT a migration runner.

Defaults to check-only. A production write requires a separate operator
approval bound to one OWNER scope, database instance, source, ACL, catalogue
and freshly restored backup. Neither secret issuance nor tenant enrollment
is performed here.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import time

from sqlalchemy import create_engine, text

from attention_router.config import settings

EXPECTED_REVISION = "0072_context_index_fence"


def role_names(*, tenant: str, owner: str, account: str) -> tuple[str, str]:
    # Bound to the exact same canonical derivation as the reviewed DDL.
    tag = sha256((tenant + "\0" + owner + "\0" + account).encode()).hexdigest()[:12]
    return "andy_ctx_rd_" + tag, "andy_ctx_wr_" + tag


def _review_rollback_state(conn):
    if conn.execute(text(
        "SELECT COUNT(*) FROM public.andy_context_fenced_tenants WHERE enabled"
    )).scalar_one():
        raise RuntimeError("ROLES_RELEASE_FENCE_ENROLLMENT_ACTIVE")
    if conn.execute(text(
        "SELECT COUNT(*) FROM public.context_consent_scopes"
    )).scalar_one():
        raise RuntimeError("ROLES_RELEASE_OWNER_GRANTS_PRESENT")
    if conn.execute(text(
        "SELECT COUNT(*) FROM public.context_source_consents"
    )).scalar_one():
        raise RuntimeError("ROLES_RELEASE_CONSENTS_PRESENT")


def _verify_effective_privileges(conn, name, *, reader, plan):
    # Includes implicit PUBLIC grants and column-only privileges. In
    # particular a consent role may write exactly the two consent tables.
    allowed_reads = {*plan["READ_TABLES"], *plan["CONSENT_TABLES"], "alembic_version"}
    allowed_updates = set(plan["LOCK_TABLES"]) | (
        {"context_consent_scopes"} if reader else set(plan["CONSENT_TABLES"])
    )
    for row in conn.execute(text("""
        SELECT c.relname, a.attname,
            has_column_privilege(:role,c.oid,a.attnum,'SELECT'),
            has_column_privilege(:role,c.oid,a.attnum,'INSERT'),
            has_column_privilege(:role,c.oid,a.attnum,'UPDATE'),
            has_table_privilege(:role,c.oid,'DELETE,TRUNCATE,TRIGGER'),
            has_column_privilege(:role,c.oid,a.attnum,'REFERENCES')
        FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
        WHERE c.relnamespace='public'::regnamespace AND c.relkind IN ('r','p','v','m','f')
          AND a.attnum>0 AND NOT a.attisdropped
    """), {"role": name}):
        table, column, select, insert, update, destructive, references = row
        may_write_consent = not reader and table in plan["CONSENT_TABLES"]
        if (select and table not in allowed_reads or insert and not may_write_consent
                or update and not (table in allowed_updates and (column == "id" or may_write_consent))
                or destructive or references):
            raise RuntimeError("ROLES_RELEASE_EXCESSIVE_PRIVILEGES")
    if conn.scalar(text("SELECT has_schema_privilege(:role,'public','CREATE')"), {"role": name}):
        raise RuntimeError("ROLES_RELEASE_EXCESSIVE_PRIVILEGES")


def _load_private_json(path: Path, *, release):
    release["private_file"](path)
    return json.loads(path.read_text(encoding="utf-8"))


def run(action, *, root: Path, acl_path: Path, catalog_path: Path,
        scope_path: Path, approval_path: Path | None, github_comment_id: int | None = None):
    if settings.app_env != "production":
        raise RuntimeError("ROLES_RELEASE_REQUIRES_PRODUCTION_MODE")
    if action not in ("check", "apply", "rollback"):
        raise ValueError("ROLES_RELEASE_ACTION_INVALID")
    if action != "check" and os.getenv("ANDY_ROLES_RELEASE_EXPLICIT") != "ROLES_ONLY_NO_TENANT":
        raise RuntimeError("ROLES_RELEASE_OPERATOR_WINDOW_NOT_OPEN")

    if root.resolve() != Path(__file__).resolve().parents[2]:
        raise RuntimeError("RELEASE_RUNNER_CHECKOUT_MISMATCH")

    import runpy
    release = runpy.run_path(str(root / "ops/context-canary/release_schema.py"))
    if action != "check":
        record = release["verify_github_release"](
            root, approval_path, github_comment_id, "roles_" + action,
        )
    plan = runpy.run_path(str(root / "ops/context-canary/role_plan.py"))
    acl = _load_private_json(acl_path, release=release)
    scope = _load_private_json(scope_path, release=release)
    if not isinstance(scope, dict) or set(scope) != {"tenant", "owner", "account", "indexer_roles"}:
        raise RuntimeError("ROLES_RELEASE_SCOPE_INVALID")
    tenant, owner, account = (scope[k] for k in ("tenant", "owner", "account"))
    # plan.generate() verifies type, length and source_account != default
    forward = plan["generate"](
        tenant=tenant, owner=owner, account=account, legacy_manifest=acl, indexer_roles=scope["indexer_roles"],
    )
    inverse = plan["generate_rollback"](
        tenant=tenant, owner=owner, account=account, legacy_manifest=acl, indexer_roles=scope["indexer_roles"],
    )
    names = role_names(tenant=tenant, owner=owner, account=account)
    engine = create_engine(
        os.environ["DATABASE_URL"], hide_parameters=True,
        connect_args={"connect_timeout": 4},
    )
    try:
        with engine.connect() as conn:
            with conn.begin():
                if action == "check":
                    conn.execute(text("SET TRANSACTION READ ONLY"))
                conn.execute(text("SET LOCAL lock_timeout='1500ms'"))
                conn.execute(text("SET LOCAL idle_in_transaction_session_timeout='60s'"))
                conn.execute(text("SET LOCAL statement_timeout='20000ms'"))
                revision = conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                if revision != EXPECTED_REVISION:
                    raise RuntimeError("ROLES_RELEASE_SCHEMA_REVISION_REQUIRED")
                _review_rollback_state(conn)
                if action in ("check", "apply"):
                    release["verify_snapshot"](
                        conn, root=root, acl_path=acl_path, catalog_path=catalog_path,
                        revision=EXPECTED_REVISION,
                    )
                present = {
                    row[0] for row in conn.execute(text(
                        "SELECT rolname FROM pg_roles WHERE rolname=ANY(:roles)"
                    ), {"roles": list(names)})
                }
                if (
                    action == "rollback" and present != set(names)
                    or action != "rollback" and present
                ):
                    raise RuntimeError("ROLES_RELEASE_ROLE_SET_MISMATCH")
                if action == "check":
                    return {
                        "status": "CHECK_ONLY_NO_DDL",
                        "approved_for_production": False,
                        "schema": revision,
                    }
                approved = release["verify_approval"](
                    approval_path, operation="roles_" + action,
                    root=root, snapshot_path=catalog_path,
                    acl_path=acl_path, conn=conn,
                )
                release["check_window"](record, approved)
                if approved.get("scope_sha256") != release["digest"](scope_path):
                    raise RuntimeError("ROLES_RELEASE_SCOPE_ATTESTATION_MISMATCH")
                release["_preflight_running_writers"](conn)
                conn.exec_driver_sql(
                    "LOCK TABLE public.andy_context_fenced_tenants, public.context_consent_scopes, "
                    "public.context_source_consents IN SHARE ROW EXCLUSIVE MODE"
                )
                _review_rollback_state(conn)

                ddl = forward if action == "apply" else inverse
                start = time.monotonic()
                for statement in ddl.splitlines():
                    if statement and not statement.startswith("--") and statement not in ("BEGIN;", "COMMIT;"):
                        if time.monotonic() - start > 45:
                            raise RuntimeError("ROLES_RELEASE_TOTAL_DURATION_EXCEEDED")
                        conn.exec_driver_sql(statement)
                if action == "rollback":
                    release["verify_snapshot"](
                        conn, root=root, acl_path=acl_path, catalog_path=catalog_path,
                        revision=EXPECTED_REVISION,
                    )
                else:
                    for name in names:
                        flags = conn.execute(text(
                            "SELECT rolsuper,rolbypassrls,rolcanlogin "
                            "FROM pg_roles WHERE rolname=:role"
                        ), {"role": name}).one()
                        _verify_effective_privileges(conn, name, reader=name == names[0], plan=plan)
                        if flags != (False, False, True):
                            raise RuntimeError("ROLES_RELEASE_ROLE_FLAGS_WRONG")
                release["check_window"](record, approved)
                if time.monotonic() - start > 45:
                    raise RuntimeError("ROLES_RELEASE_TOTAL_DURATION_EXCEEDED")
                return {
                    "status": "ROLE_RELEASE_COMMITTED",
                    "operation": action,
                    "schema": revision,
                }
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("check", "apply", "rollback"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--acl", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--scope", required=True, type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--github-comment-id", type=int)
    args = parser.parse_args()
    result = run(
        args.action, root=args.root.resolve(), acl_path=args.acl,
        catalog_path=args.catalog, scope_path=args.scope,
        approval_path=args.approval, github_comment_id=args.github_comment_id,
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        import runpy
        release = runpy.run_path(str(Path(__file__).with_name("release_schema.py")))
        raise SystemExit(release["report_failure"](error)) from None
