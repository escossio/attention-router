"""Render a REVIEW-ONLY role/RLS plan. This program never connects to a DB.

The emitted DDL must be reviewed against existing RLS policies and writer
roles before anyone applies it, even in staging. No credential is generated.
"""

from __future__ import annotations

import re
from hashlib import sha256

from psycopg import sql
from sqlalchemy import text

LOCK_TABLES = (
    "actor_bindings", "artifacts", "artifact_receipts",
    "candidate_insight_evidence", "candidate_insights", "canonical_events",
    "client_devices", "client_sessions", "client_tenant_memberships",
    "conversation_messages", "conversation_threads", "inbound_events",
    "resources", "semantic_episodes", "tenants", "timeline_events",
    "voice_transcriptions",
)
READ_TABLES = (
    *LOCK_TABLES, "audit_events", "memory_actors", "relationships",
    "semantic_episode_memberships", "conversation_vector_segments",
    "conversation_vector_memberships", "conversation_embeddings",
)
CONSENT_TABLES = ("context_consent_scopes", "context_source_consents")


PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")
LEGACY_ROLE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")


def access_check(operation):
    # RLS applies to column-granted operations too. The policy must preserve
    # row visibility while PostgreSQL continues to enforce individual columns.
    if operation == "DELETE":
        return "pg_catalog.has_table_privilege"
    return "pg_catalog.has_any_column_privilege"


def require_legacy_manifest(manifest):
    """Fail closed if an audited pre-RLS ACL inventory is incomplete."""
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("CANARY_LEGACY_MANIFEST_INVALID")
    roles = manifest.get("roles")
    states = manifest.get("tables")
    if (
        not isinstance(roles, list) or not roles
        or len(set(roles)) != len(roles)
        or any(not isinstance(role, str) or not LEGACY_ROLE_PATTERN.fullmatch(role)
               for role in roles)
        or not isinstance(states, dict) or set(states) != set(READ_TABLES)
    ):
        raise ValueError("CANARY_LEGACY_MANIFEST_INCOMPLETE")
    for table, state in states.items():
        if (
            not isinstance(state, dict) or type(state.get("rls")) is not bool
            or not isinstance(state.get("roles"), dict)
            or set(state["roles"]) != set(roles)
        ):
            raise ValueError("CANARY_LEGACY_MANIFEST_INVALID")
        for role, flags in state["roles"].items():
            if not isinstance(flags, dict) or set(flags) != set(PRIVILEGES):
                raise ValueError("CANARY_LEGACY_MANIFEST_INVALID")
            if any(type(value) is not bool for value in flags.values()):
                raise ValueError("CANARY_LEGACY_MANIFEST_INVALID")
    return roles, states


def audit_catalog(conn, *, legacy_roles):
    """Read-only pg_catalog snapshot; no passwords, tokens or row content.

    The caller owns the connection and must provide a read-only transaction
    when examining an operational database. Synthetic CI fixtures may audit
    inside a transaction that also created mock legacy roles.
    """
    if not isinstance(legacy_roles, (list, tuple)) or not legacy_roles:
        raise ValueError("CANARY_LEGACY_ROLES_REQUIRED")
    result = {"version": 1, "roles": list(legacy_roles), "tables": {}}
    for role in legacy_roles:
        role_flags = conn.execute(text(
            "SELECT rolsuper,rolbypassrls FROM pg_catalog.pg_roles "
            "WHERE rolname=:role"
        ), {"role": role}).one_or_none()
        if role_flags is None or any(role_flags):
            raise ValueError("CANARY_LEGACY_ROLE_NOT_RESTRICTED")
    for table in READ_TABLES:
        qualified = "public." + table
        enabled = conn.execute(text(
            "SELECT relrowsecurity FROM pg_catalog.pg_class "
            "WHERE oid=pg_catalog.to_regclass(:table)"
        ), {"table": qualified}).scalar_one_or_none()
        if type(enabled) is not bool:
            raise ValueError("CANARY_LEGACY_TABLE_NOT_FOUND")
        role_flags = {}
        for role in legacy_roles:
            flags = {}
            for operation in PRIVILEGES:
                allowed = conn.execute(text(
                    f"SELECT {access_check(operation)}(:role,:table,:operation)"
                ), {"role": role, "table": qualified, "operation": operation}).scalar_one()
                if type(allowed) is not bool:
                    raise ValueError("CANARY_LEGACY_ACL_UNAVAILABLE")
                flags[operation] = allowed
            role_flags[role] = flags
        result["tables"][table] = {"rls": enabled, "roles": role_flags}
    require_legacy_manifest(result)
    return result


def legacy_preflight(manifest, form):
    """Emit one-line, transaction-scoped catalog predicates, before any RLS."""
    roles, states = require_legacy_manifest(manifest)
    statements = []
    for table in READ_TABLES:
        state = states[table]
        checks = [sql.SQL(
            "(SELECT relrowsecurity FROM pg_catalog.pg_class "
            "WHERE oid={table}::regclass) IS DISTINCT FROM {expected}"
        ).format(
            table=sql.Literal("public." + table),
            expected=sql.Literal(state["rls"]),
        )]
        for role in roles:
            for operation in PRIVILEGES:
                checks.append(sql.SQL(
                    "{check}({role},{table},{priv}) IS DISTINCT FROM {expected}"
                ).format(
                    check=sql.SQL(access_check(operation)),
                    role=sql.Literal(role),
                    table=sql.Literal("public." + table),
                    priv=sql.Literal(operation),
                    expected=sql.Literal(state["roles"][role][operation]),
                ))
        condition = sql.SQL(" OR ").join(checks)
        statements.append(form(
            "DO $$ BEGIN IF {condition} THEN "
            "RAISE EXCEPTION 'CANARY_LEGACY_CATALOG_DRIFT'; "
            "END IF; END $$",
            condition=condition,
        ))
    return statements


def legacy_compatible_policies(table_name, state, form, tag):
    """Restore only previously granted behavior on tables with RLS formerly off.

    Never add policies to pre-existing RLS tables, particularly the original
    pgvector policies based on andy.vector_tenant/account session GUCs.
    Adding a permissive role policy there could bypass the old GUC restriction.
    """
    if state["rls"]:
        return []
    statements = []
    table = sql.Identifier("public", table_name)
    for privilege in PRIVILEGES:
        principals = sorted(
            role for role, flags in state["roles"].items() if flags[privilege]
        )
        if not principals:
            continue
        target = sql.SQL(", ").join(sql.Identifier(role) for role in principals)
        policy = sql.Identifier(
            "ctx_" + tag + "_" + table_name[:15] + "_legacy_" + privilege.lower()
        )
        if privilege == "SELECT":
            command = "FOR SELECT TO {principals} USING (true)"
        elif privilege == "INSERT":
            command = "FOR INSERT TO {principals} WITH CHECK (true)"
        elif privilege == "UPDATE":
            command = "FOR UPDATE TO {principals} USING (true) WITH CHECK (true)"
        else:
            command = "FOR DELETE TO {principals} USING (true)"
        statements.append(form(
            "CREATE POLICY {policy} ON {table} AS PERMISSIVE " + command,
            policy=policy, table=table, principals=target,
        ))
    return statements


def indexer_grants(roles, manifest, *, rollback=False):
    # Each named indexer is part of the scope attested by the operator. It
    # must already be an audited, restricted vector writer. No base-table
    # privileges are added. Refuse pre-existing grants that rollback would erase.
    if (not isinstance(roles, list) or any(role not in manifest["roles"] for role in roles)
            or len(roles) != len(set(roles))):
        raise ValueError("CANARY_INDEXER_ROLE_INVALID")
    result = []
    for role in roles:
        principal = sql.Identifier(role)
        args = {"role": sql.Literal(role)}
        statements = [
            """DO $$ BEGIN IF NOT (
                pg_catalog.has_table_privilege({role},'public.conversation_messages','SELECT') AND
                pg_catalog.has_table_privilege({role},'public.conversation_vector_segments','INSERT') AND
                pg_catalog.has_table_privilege({role},'public.conversation_embeddings','INSERT') AND
                pg_catalog.has_column_privilege({role},'public.queue','payload','UPDATE')
            ) OR pg_catalog.has_table_privilege({role},'public.andy_vector_attachment_provenance','SELECT')
               OR pg_catalog.has_function_privilege({role},'public.andy_context_index_writer_lock(text)','EXECUTE')
            THEN RAISE EXCEPTION 'CANARY_INDEXER_ACL_DRIFT'; END IF; END $$""",
            "GRANT SELECT ON public.andy_vector_attachment_provenance TO {principal}",
            "GRANT EXECUTE ON FUNCTION public.andy_context_index_writer_lock(text) TO {principal}",
        ]
        if rollback:
            statements = [
                "REVOKE SELECT ON public.andy_vector_attachment_provenance FROM {principal}",
                "REVOKE EXECUTE ON FUNCTION public.andy_context_index_writer_lock(text) FROM {principal}",
            ]
        for statement in statements:
            result.append(sql.SQL(" ".join(statement.splitlines())).format(
                **args, principal=principal,
            ).as_string() + ";")
    return result


def generate(*, tenant: str, owner: str, account: str, legacy_manifest: dict,
             indexer_roles=None) -> str:
    if not (
        all(isinstance(value, str) and value.isprintable() for value in (tenant, owner, account))
        and 0 < len(tenant) <= 64
        and 0 < len(owner) <= 64
        and 0 < len(account) <= 120 and account != "default"
    ):
        raise ValueError("CANARY_RLS_SCOPE_INVALID")

    tag = sha256((tenant + "\x00" + owner + "\x00" + account).encode()).hexdigest()[:12]
    reader = f"andy_ctx_rd_{tag}"
    writer = f"andy_ctx_wr_{tag}"
    roles = sql.SQL(", ").join([sql.Identifier(reader), sql.Identifier(writer)])
    qtenant, qowner, qaccount = map(sql.Literal, (tenant, owner, account))

    def form(statement, **kwargs):
        return sql.SQL(statement).format(**kwargs).as_string() + ";"

    def table(name):
        return sql.Identifier("public", name)

    def name(suffix):
        return sql.Identifier(f"ctx_{tag}_{suffix}")

    def rule(tablename):
        if tablename == "tenants":
            return sql.SQL("id={tenant}").format(tenant=qtenant)
        if tablename == "client_devices":
            return sql.SQL("human_identity_id={owner}").format(owner=qowner)
        if tablename in ("client_tenant_memberships", "client_sessions"):
            return sql.SQL("tenant_id={tenant} AND human_identity_id={owner}").format(
                tenant=qtenant, owner=qowner,
            )
        if tablename in (
            "conversation_messages", "conversation_threads", "artifact_receipts",
            "conversation_vector_segments", "conversation_vector_memberships",
            "conversation_embeddings",
        ):
            return sql.SQL("tenant_id={tenant} AND source_account={account}").format(
                tenant=qtenant, account=qaccount,
            )
        if tablename == "context_consent_scopes":
            return sql.SQL(
                "tenant_id={tenant} AND owner_id={owner} AND source_account={account}"
            ).format(tenant=qtenant, owner=qowner, account=qaccount)
        if tablename == "context_source_consents":
            return sql.SQL(
                "EXISTS (SELECT 1 FROM public.context_consent_scopes AS cs "
                "WHERE cs.id=scope_id AND cs.tenant_id={tenant} "
                "AND cs.owner_id={owner} AND cs.source_account={account})"
            ).format(tenant=qtenant, owner=qowner, account=qaccount)
        return sql.SQL("tenant_id={tenant}").format(tenant=qtenant)

    legacy_roles, states = require_legacy_manifest(legacy_manifest)
    lines = [
        "-- REVIEW ONLY. Existing RLS/ACL catalog is checked before DDL.",
        "-- Login roles have PASSWORD NULL. Supply SCRAM secrets separately, never in generated SQL.",
        "-- Enabling RLS on shared tables may affect other non-superuser clients.",
        "BEGIN;",
    ]
    # The snapshot and current PostgreSQL catalog must match, or nothing
    # from this transaction may be promoted.
    lines.extend(legacy_preflight(legacy_manifest, form))
    lines.extend(indexer_grants([] if indexer_roles is None else indexer_roles, legacy_manifest))
    for role in (reader, writer):
        lines.append(form(
            "CREATE ROLE {role} LOGIN PASSWORD NULL NOINHERIT NOSUPERUSER NOBYPASSRLS "
            "NOCREATEROLE NOCREATEDB NOREPLICATION",
            role=sql.Identifier(role),
        ))
    lines.append(form("GRANT USAGE ON SCHEMA public TO {roles}", roles=roles))
    lines.append(form(
        "GRANT SELECT ON public.alembic_version TO {roles}", roles=roles,
    ))
    lines.append(form(
        "GRANT EXECUTE ON FUNCTION public.andy_context_index_lock(text,boolean) TO {roles}",
        roles=roles,
    ))

    for tablename in (*READ_TABLES, *CONSENT_TABLES):
        t = table(tablename)
        r = rule(tablename)
        if tablename in states:
            # Restore SELECT/INSERT/UPDATE/DELETE access of *existing roles*
            # only when the table was previously RLS-disabled. Never grant
            # additional table privileges to those principals.
            lines.extend(legacy_compatible_policies(tablename, states[tablename], form, tag))
        lines.extend([
            form("ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", table=t),
            form("GRANT SELECT ON {table} TO {roles}", table=t, roles=roles),
            form("CREATE POLICY {policy} ON {table} AS PERMISSIVE FOR SELECT TO {roles} "
                 "USING (true)", policy=name(tablename[:15] + "_read"), table=t, roles=roles),
            form("CREATE POLICY {policy} ON {table} AS RESTRICTIVE FOR SELECT TO {roles} "
                 "USING ({rule})", policy=name(tablename[:15] + "_guard"), table=t, roles=roles, rule=r),
        ])
        if tablename in LOCK_TABLES or tablename == "context_consent_scopes":
            locking_roles = roles if tablename in LOCK_TABLES else sql.Identifier(reader)
            lines.extend([
                form("GRANT UPDATE(id) ON {table} TO {roles}", table=t, roles=locking_roles),
                form("CREATE POLICY {policy} ON {table} FOR UPDATE TO {roles} "
                     "USING ({rule}) WITH CHECK(false)",
                     policy=name(tablename[:15] + "_lock"), table=t, roles=locking_roles, rule=r),
                form("CREATE POLICY {policy} ON {table} AS RESTRICTIVE FOR UPDATE TO {roles} "
                     "USING ({rule}) WITH CHECK(false)",
                     policy=name(tablename[:15] + "_lock_guard"), table=t, roles=locking_roles, rule=r),
            ])
        if tablename in CONSENT_TABLES:
            lines.extend([
                form("GRANT INSERT, UPDATE ON {table} TO {writer}", table=t, writer=sql.Identifier(writer)),
                form("CREATE POLICY {policy} ON {table} FOR INSERT TO {writer} WITH CHECK ({rule})",
                     policy=name(tablename[:15] + "_insert"), table=t, writer=sql.Identifier(writer), rule=r),
                form("CREATE POLICY {policy} ON {table} FOR UPDATE TO {writer} "
                     "USING ({rule}) WITH CHECK ({rule})",
                     policy=name(tablename[:15] + "_update"), table=t, writer=sql.Identifier(writer), rule=r),
            ])
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def generate_rollback(*, tenant: str, owner: str, account: str,
                      legacy_manifest: dict, indexer_roles=None) -> str:
    """Review-only inverse for canary roles/policies, never source data.

    This does NOT roll back Alembic migrations. It refuses a live enrolled
    canary and fails atomically if any canary role has acquired additional
    dependencies/owned objects since its creation. It cannot erase consent.
    """
    require_legacy_manifest(legacy_manifest)
    # Apply the same strict scope validation as the forward plan.
    generate(tenant=tenant, owner=owner, account=account,
             legacy_manifest=legacy_manifest)
    tag = sha256((tenant + "\x00" + owner + "\x00" + account).encode()).hexdigest()[:12]
    reader = sql.Identifier(f"andy_ctx_rd_{tag}")
    writer = sql.Identifier(f"andy_ctx_wr_{tag}")
    both = sql.SQL(", ").join((reader, writer))

    def form(statement, **kwargs):
        return sql.SQL(statement).format(**kwargs).as_string() + ";"

    def table(name):
        return sql.Identifier("public", name)

    def name(suffix):
        return sql.Identifier(f"ctx_{tag}_{suffix}")

    lines = [
        "-- REVIEW ONLY. All attached connections must be drained.",
        "-- Separate Alembic downgrade follows only after conditional data checks.",
        "BEGIN;",
        form("DO $$ BEGIN IF EXISTS (SELECT 1 FROM "
             "public.andy_context_fenced_tenants WHERE enabled IS TRUE) "
             "THEN RAISE EXCEPTION 'CANARY_ROLLBACK_DRAIN_REQUIRED'; "
             "END IF; END $$"),
    ]
    for tablename in (*READ_TABLES, *CONSENT_TABLES):
        t = table(tablename)
        if tablename in READ_TABLES and not legacy_manifest["tables"][tablename]["rls"]:
            for privilege in PRIVILEGES:
                participants = sorted(
                    role for role, rights in
                    legacy_manifest["tables"][tablename]["roles"].items()
                    if rights[privilege]
                )
                if participants:
                    lines.append(form(
                        "DROP POLICY {policy} ON {table}",
                        policy=sql.Identifier(
                            f"ctx_{tag}_{tablename[:15]}_legacy_{privilege.lower()}"
                        ),
                        table=t,
                    ))

        for suffix in ("_read", "_guard"):
            lines.append(form(
                "DROP POLICY {policy} ON {table}",
                policy=name(tablename[:15] + suffix), table=t,
            ))
        if tablename in LOCK_TABLES or tablename == "context_consent_scopes":
            lines.append(form("DROP POLICY {policy} ON {table}",
                              policy=name(tablename[:15] + "_lock_guard"), table=t))
            lines.append(form(
                "DROP POLICY {policy} ON {table}",
                policy=name(tablename[:15] + "_lock"), table=t,
            ))
        if tablename in CONSENT_TABLES:
            for suffix in ("_insert", "_update"):
                lines.append(form(
                    "DROP POLICY {policy} ON {table}",
                    policy=name(tablename[:15] + suffix), table=t,
                ))

        lines.append(form("REVOKE SELECT ON {table} FROM {roles}",
                          table=t, roles=both))
        if tablename in LOCK_TABLES:
            lines.append(form("REVOKE UPDATE(id) ON {table} FROM {roles}",
                              table=t, roles=both))
        if tablename == "context_consent_scopes":
            lines.append(form("REVOKE UPDATE(id) ON {table} FROM {reader}",
                              table=t, reader=reader))
        if tablename in CONSENT_TABLES:
            lines.append(form("REVOKE INSERT, UPDATE ON {table} FROM {writer}",
                              table=t, writer=writer))
        if tablename in READ_TABLES and not legacy_manifest["tables"][tablename]["rls"]:
            lines.append(form("ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
                              table=t))

    lines.extend(indexer_grants([] if indexer_roles is None else indexer_roles, legacy_manifest, rollback=True))
    lines.extend([
        form("REVOKE SELECT ON public.alembic_version FROM {roles}", roles=both),
        form("REVOKE EXECUTE ON FUNCTION "
             "public.andy_context_index_lock(text,boolean) FROM {roles}", roles=both),
        form("REVOKE USAGE ON SCHEMA public FROM {roles}", roles=both),
        form("DROP ROLE {reader}", reader=reader),
        form("DROP ROLE {writer}", writer=writer),
        "COMMIT;",
    ])
    return "\n".join(lines) + "\n"
