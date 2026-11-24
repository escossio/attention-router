"""Promotion checks are metadata-only, fail-closed and never executable DDL."""

from copy import deepcopy
from pathlib import Path
import runpy

import pytest


ROOT = Path(__file__).resolve().parents[1]
review = runpy.run_path(str(ROOT / "ops/context-canary/preflight_review.py"))
role_plan = runpy.run_path(str(ROOT / "ops/context-canary/role_plan.py"))


def synthetic():
    states = {}
    access = {}
    for table in role_plan["READ_TABLES"]:
        is_vector = table in review["VECTOR_TABLES"]
        states[table] = (is_vector, is_vector)
    for role in ("synthetic_index", "synthetic_monitor"):
        access[role] = []
    access["synthetic_index"] = [
        ["conversation_messages", "SELECT"],
        ["conversation_vector_segments", "SELECT"],
    ]
    access["synthetic_monitor"] = [
        ["conversation_messages", "SELECT"],
        ["conversation_vector_segments", "SELECT"],
    ]
    acl = {
        "version": 1, "roles": list(access), "tables": {
            table: {
                "rls": bool(state[0]),
                "roles": {
                    role: {
                        operation: [table, operation] in access[role]
                        for operation in role_plan["PRIVILEGES"]
                    }
                    for role in access
                },
            }
            for table, state in states.items()
        },
    }
    contacts = review["expected_contacts"](ROOT)
    snapshot = {
        "version": 1,
        "schema_revision": "0067_vector_lock_order",
        "rls_tables": [[table, *value] for table, value in states.items()],
        "effective_access_roles": access,
        "contact_columns": [
            [table, column, kind, "NO" if not_nullable else "YES", length]
            for (table, column), (kind, not_nullable, length) in contacts.items()
        ],
        "contact_indexes": [
            ["google_contacts_cache", "google_contacts_cache_pkey"],
            ["google_contacts_connections", "google_contacts_connections_pkey"],
            ["google_contacts_connections", "ix_google_contacts_connections_tenant_id"],
        ],
        "vector_policies": [
            [table, "vector_scope", "PERMISSIVE", ["public"], "ALL",
             review["VECTOR_SCOPE_EXPRESSION"],
             review["VECTOR_SCOPE_EXPRESSION"]]
            for table in review["VECTOR_TABLES"]
        ],
        "memberships": [],
        "long_transactions": 0,
        "extensions": ["vector"],
    }
    return snapshot, acl


def test_pass_for_staging_review_but_never_approve_production():
    snap, acl = synthetic()
    result = review["evaluate"](snap, acl, root=ROOT)
    assert result["status"] == "PASS_FOR_DISPOSABLE_REHEARSAL"
    assert result["blockers"] == []
    assert result["approved_for_production"] is False


@pytest.mark.parametrize("defect,expected", [
    ("revision", "SCHEMA_REVISION_MISMATCH"),
    ("contact", "GOOGLE_CONTACTS_0068_SCHEMA_DIFFERENCE"),
    ("missing_index", "GOOGLE_CONTACTS_0068_INDEX_MISSING"),
    ("unknown_role", "LEGACY_ROLE_SET_DRIFT"),
    ("privileges", "LEGACY_PRIVILEGES_DRIFT"),
    ("vector", "VECTOR_POLICY_SEMANTICS_DRIFT"),
    ("vector_rls", "VECTOR_RLS_FORCE_DRIFT"),
    ("extension", "PGVECTOR_EXTENSION_MISSING"),
    ("role_membership", "LEGACY_ROLE_MEMBERSHIP_NOT_REVIEWED"),
])
def test_all_material_drift_refuses_promotion(defect, expected):
    snap, acl = synthetic()
    snap = deepcopy(snap)
    if defect == "revision":
        snap["schema_revision"] = "0072_context_index_fence"
    elif defect == "contact":
        snap["contact_columns"][0][2] = "bytea"
    elif defect == "missing_index":
        snap["contact_indexes"].pop()
    elif defect == "unknown_role":
        snap["effective_access_roles"]["other"] = []
    elif defect == "privileges":
        snap["effective_access_roles"]["synthetic_index"] = []
    elif defect == "vector":
        snap["vector_policies"][0][5] = "true"
    elif defect == "vector_rls":
        for row in snap["rls_tables"]:
            if row[0] in review["VECTOR_TABLES"]:
                row[2] = False
    elif defect == "extension":
        snap["extensions"].remove("vector")
    elif defect == "role_membership":
        snap["memberships"] = [["legacy_monitor", "unknown_role"]]
    result = review["evaluate"](snap, acl, root=ROOT)
    assert result["status"] == "BLOCKED"
    assert result["approved_for_production"] is False
    assert expected in result["blockers"]
