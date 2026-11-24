"""Reject unsafe promotion assumptions using read-only production metadata.

This module never connects to a database and cannot apply DDL. Results are
a PREPARATION gate, never authorization for a production migration.
"""

from __future__ import annotations

import json
from pathlib import Path
import runpy

from sqlalchemy import Boolean, Float, Integer, JSON, String, Text


VECTOR_TABLES = (
    "conversation_vector_segments",
    "conversation_vector_memberships",
    "conversation_embeddings",
)

VECTOR_SCOPE_EXPRESSION = (
    "(((tenant_id)::text = current_setting('andy.vector_tenant'::text, true)) "
    "AND ((source_account)::text = "
    "current_setting('andy.vector_account'::text, true)))"
)


def expected_contacts(root: Path):
    migration = runpy.run_path(
        str(root / "alembic/versions/0068_google_contacts_cache.py")
    )
    expected = {}
    for table in (migration["connections"], migration["cache"]):
        for col in table.columns:
            typ = col.type
            if isinstance(typ, Text):
                kind, length = "text", None
            elif isinstance(typ, String):
                kind, length = "character varying", typ.length
            elif isinstance(typ, JSON):
                kind, length = "json", None
            elif isinstance(typ, Float):
                kind, length = "double precision", None
            elif isinstance(typ, Integer):
                kind, length = "integer", None
            elif isinstance(typ, Boolean):
                kind, length = "boolean", None
            else:
                raise ValueError("CANARY_CONTACT_TYPE_UNSUPPORTED")
            expected[(table.name, col.name)] = (kind, not col.nullable, length)
    return expected


def evaluate(snapshot, acl, *, root: Path, revision="0067_vector_lock_order"):
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location(
        "canary_role_plan_for_review", root / "ops/context-canary/role_plan.py"
    )
    role_module = module_from_spec(spec)
    spec.loader.exec_module(role_module)
    role_module.require_legacy_manifest(acl)
    blockers = []
    def fail(code):
        if code not in blockers:
            blockers.append(code)

    if snapshot.get("version") != 1 or snapshot.get("schema_revision") != revision:
        fail("SCHEMA_REVISION_MISMATCH")
    expected = expected_contacts(root)
    actual = {
        (row[0], row[1]): (row[2], row[3] == "NO", row[4])
        for row in snapshot.get("contact_columns", [])
        if isinstance(row, list) and len(row) == 5
    }
    if expected != actual:
        fail("GOOGLE_CONTACTS_0068_SCHEMA_DIFFERENCE")
    idx = set(tuple(row) for row in snapshot.get("contact_indexes", []))
    for item in (
        ("google_contacts_cache", "google_contacts_cache_pkey"),
        ("google_contacts_connections", "google_contacts_connections_pkey"),
        ("google_contacts_connections", "ix_google_contacts_connections_tenant_id"),
    ):
        if item not in idx:
            fail("GOOGLE_CONTACTS_0068_INDEX_MISSING")
    state = {row[0]: (row[1], row[2]) for row in snapshot.get("rls_tables", [])}
    if set(state) != set(role_module.READ_TABLES):
        fail("CATALOG_TABLE_SET_DRIFT")
    else:
        for name, values in state.items():
            if values[0] != acl["tables"][name]["rls"]:
                fail("RLS_STATE_DRIFT")
            if name in VECTOR_TABLES and values != (True, True):
                fail("VECTOR_RLS_FORCE_DRIFT")
    current_roles = snapshot.get("effective_access_roles", {})
    if set(current_roles) != set(acl["roles"]):
        fail("LEGACY_ROLE_SET_DRIFT")
    for role in acl["roles"]:
        expected_privs = {
            (name, operation)
            for name, item in acl["tables"].items()
            for operation, enabled in item["roles"][role].items()
            if enabled
        }
        got = {tuple(v) for v in current_roles.get(role, [])}
        if expected_privs != got:
            fail("LEGACY_PRIVILEGES_DRIFT")

    policies = snapshot.get("vector_policies", [])
    if len(policies) != len(VECTOR_TABLES) or {
        p[0] for p in policies
    } != set(VECTOR_TABLES):
        fail("VECTOR_POLICIES_INCOMPLETE")
    for row in policies:
        if len(row) != 7:
            fail("VECTOR_POLICY_METADATA_INVALID")
            continue
        if (
            row[1] != "vector_scope"
            or row[2] != "PERMISSIVE"
            or row[3] != ["public"]
            or row[4] != "ALL"
            or row[5] != VECTOR_SCOPE_EXPRESSION
            or row[6] != VECTOR_SCOPE_EXPRESSION
        ):
            fail("VECTOR_POLICY_SEMANTICS_DRIFT")
    # Roles could silently inherit privileges through role membership.
    # No such memberships exist in the observed operational baseline;
    # require a distinct review if a role grant appears.
    if snapshot.get("memberships") != []:
        fail("LEGACY_ROLE_MEMBERSHIP_NOT_REVIEWED")

    # A zero count is a point-in-time observation, not a future lock budget.
    active_long_xacts = snapshot.get("long_transactions", -1)
    if not isinstance(active_long_xacts, int) or active_long_xacts < 0:
        fail("TRANSACTION_OBSERVABILITY_UNAVAILABLE")
    extensions = set(snapshot.get("extensions", []))
    if "vector" not in extensions:
        fail("PGVECTOR_EXTENSION_MISSING")

    return {
        "status": "PASS_FOR_DISPOSABLE_REHEARSAL" if not blockers else "BLOCKED",
        "approved_for_production": False,
        "blockers": blockers,
        "table_count": len(state),
        "vector_policy_count": len(policies),
        "legacy_role_count": len(current_roles),
        "contact_column_count": len(actual),
        "observed_long_transactions": active_long_xacts,
    }


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--legacy-acl", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    args = parser.parse_args()
    for source in (args.catalog, args.legacy_acl):
        if not source.is_file() or source.stat().st_mode & 0o077:
            parser.error("Catalog evidence must be a private mode-0600 file")
    result = evaluate(
        json.loads(args.catalog.read_text()),
        json.loads(args.legacy_acl.read_text()),
        root=args.source_root,
    )
    print(json.dumps(result, sort_keys=True))
    if result["blockers"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
