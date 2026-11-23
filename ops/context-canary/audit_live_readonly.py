"""One-shot read-only catalog inventory for an authorized private canary gate.

The authorized operator selects a new private directory outside the checkout.
Only catalog and ACL metadata are written there, in one read-only DB snapshot.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy

from sqlalchemy import create_engine, text

EXPECTED_TABLES = (
    "actor_bindings", "artifacts", "artifact_receipts", "candidate_insight_evidence",
    "candidate_insights", "canonical_events", "client_devices", "client_sessions",
    "client_tenant_memberships", "conversation_messages", "conversation_threads",
    "inbound_events", "resources", "semantic_episodes", "tenants", "timeline_events",
    "voice_transcriptions", "audit_events", "memory_actors", "relationships",
    "semantic_episode_memberships", "conversation_vector_segments",
    "conversation_vector_memberships", "conversation_embeddings",
)
VECTOR_TABLES = (
    "conversation_vector_segments", "conversation_vector_memberships",
    "conversation_embeddings",
)
CONTACT_TABLES = ("google_contacts_connections", "google_contacts_cache")


def context_object_acls(conn):
    # Compare effective ACL entries. After GRANT/REVOKE PostgreSQL can store
    # an explicit default ACL where the original catalog field was NULL.
    rows = conn.execute(text("""
        WITH objects AS (
          SELECT c.relname AS name, coalesce(c.relacl, acldefault('r',c.relowner)) AS acl
          FROM pg_class c WHERE c.relnamespace='public'::regnamespace AND c.relname IN
            ('andy_vector_attachment_provenance','andy_context_fenced_tenants',
             'context_consent_scopes','context_source_consents','alembic_version')
          UNION ALL
          SELECT p.proname, coalesce(p.proacl, acldefault('f',p.proowner)) FROM pg_proc p
          WHERE p.pronamespace='public'::regnamespace AND p.proname IN
            ('andy_context_index_lock','andy_context_index_writer_lock','andy_context_index_enrolled',
             'andy_context_index_row','andy_context_index_truncate')
        )
        SELECT name, grantor::regrole::text,
               CASE WHEN grantee=0 THEN 'PUBLIC' ELSE grantee::regrole::text END,
               privilege_type, is_grantable
        FROM objects CROSS JOIN LATERAL aclexplode(acl)
        ORDER BY 1,2,3,4,5
    """)).all()
    return [list(row) for row in rows]


def snapshot(conn):
    version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    columns = conn.execute(text("""
        SELECT table_name,column_name,data_type,is_nullable,character_maximum_length
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=ANY(:tables)
        ORDER BY table_name,ordinal_position
    """), {"tables": list(CONTACT_TABLES)}).all()
    contact_indexes = conn.execute(text("""
        SELECT tablename,indexname
        FROM pg_indexes WHERE schemaname='public' AND tablename=ANY(:tables)
        ORDER BY tablename,indexname
    """), {"tables": list(CONTACT_TABLES)}).all()
    policies = conn.execute(text("""
        SELECT tablename,policyname,permissive,roles,cmd,qual,with_check
        FROM pg_policies WHERE schemaname='public' AND tablename=ANY(:tables)
        ORDER BY tablename,policyname
    """), {"tables": list(VECTOR_TABLES)}).all()
    tbl = conn.execute(text("""
        SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity
        FROM pg_class c
        WHERE c.relnamespace='public'::regnamespace
          AND c.relkind IN ('r','p') AND c.relname=ANY(:tables)
        ORDER BY c.relname
    """), {"tables": list(EXPECTED_TABLES)}).all()
    all_roles = conn.execute(text("""
        SELECT rolname,rolsuper,rolbypassrls,rolcanlogin
        FROM pg_roles WHERE rolname NOT LIKE 'pg_%'
        ORDER BY rolname
    """)).all()
    inherited = conn.execute(text("""
        SELECT pg_get_userbyid(roleid),pg_get_userbyid(member)
        FROM pg_auth_members WHERE pg_get_userbyid(member) NOT LIKE 'pg_%'
        ORDER BY roleid,member
    """)).all()
    users = [r[0] for r in all_roles if not r[1] and not r[2]]
    privileges = {}
    for role in users:
        if role == "postgres":
            continue
        access = []
        for table in EXPECTED_TABLES:
            for operation in ("SELECT","INSERT","UPDATE","DELETE"):
                check = "pg_catalog.has_table_privilege" if operation == "DELETE" else "pg_catalog.has_any_column_privilege"
                permitted = conn.execute(text(
                    f"SELECT {check}(:role,:table,:op)"
                ), {"role": role, "table": "public." + table,
                    "op": operation}).scalar()
                if permitted:
                    access.append([table,operation])
        if access:
            privileges[role] = access

    # Capture direct column grants and policy definitions as private metadata.
    # Comparing table-level booleans alone cannot detect grants moving columns.
    column_acls = conn.execute(text("""
        SELECT c.relname,a.attname,grantor::regrole::text,
               CASE WHEN grantee=0 THEN 'PUBLIC' ELSE grantee::regrole::text END,
               privilege_type,is_grantable FROM pg_catalog.pg_attribute a
        JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
        CROSS JOIN LATERAL aclexplode(a.attacl)
        WHERE c.relnamespace='public'::regnamespace AND c.relname=ANY(:tables)
          AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY 1,2,3,4,5,6
    """), {"tables": list(EXPECTED_TABLES)}).all()
    shared_policies = conn.execute(text("""
        SELECT tablename,policyname,permissive,roles,cmd,qual,with_check
        FROM pg_policies WHERE schemaname='public' AND tablename=ANY(:tables)
        ORDER BY tablename,policyname
    """), {"tables": list(EXPECTED_TABLES)}).all()

    # Extra state that matters for lock-window planning but contains no
    # personal or message content.
    sessions = conn.execute(text("""
        SELECT state, count(*) FROM pg_stat_activity
        WHERE datname=current_database() GROUP BY state ORDER BY state
    """)).all()
    long_xacts = conn.execute(text("""
        SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()
        AND xact_start IS NOT NULL AND clock_timestamp()-xact_start > interval '20 seconds'
    """)).scalar_one()
    extensions = conn.execute(text(
        "SELECT extname FROM pg_extension ORDER BY extname"
    )).scalars().all()

    return {
        "version": 1, "schema_revision": version,
        "contact_columns": [list(r) for r in columns],
        "contact_indexes": [list(r) for r in contact_indexes],
        "vector_policies": [list(r) for r in policies],
        "rls_tables": [list(r) for r in tbl],
        "roles": [list(r) for r in all_roles],
        "memberships": [list(r) for r in inherited],
        "effective_access_roles": privileges,
        "context_object_acls": context_object_acls(conn),
        "column_acls": [list(row) for row in column_acls],
        "shared_policies": [list(row) for row in shared_policies],
        "sessions": [list(r) for r in sessions],
        "long_transactions": long_xacts,
        "extensions": list(extensions),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if args.output.resolve().is_relative_to(root):
        raise RuntimeError("RELEASE_PRIVATE_OUTPUT_OUTSIDE_CHECKOUT_REQUIRED")
    if os.environ.get("ANDY_CANARY_READONLY_AUDIT") != "yes":
        raise RuntimeError("CANARY_READONLY_AUDIT_DISABLED")
    from attention_router.config import settings

    engine = create_engine(
        settings.database_url, hide_parameters=True, connect_args={"connect_timeout": 4}
    )
    try:
        with engine.connect() as conn, conn.begin():
            conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            conn.execute(text("SET LOCAL statement_timeout='4500ms'"))
            conn.execute(text("SET LOCAL lock_timeout='250ms'"))
            result = snapshot(conn)
            plan = runpy.run_path(str(root / "ops/context-canary/role_plan.py"))
            acl = plan["audit_catalog"](conn, legacy_roles=sorted(result["effective_access_roles"]))
        args.output.mkdir(mode=0o700, exist_ok=False)
        for name, data in (("catalog.json", result), ("acl.json", acl)):
            fd = os.open(args.output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True)
        print("CATALOG_AND_ACL_CAPTURED")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
