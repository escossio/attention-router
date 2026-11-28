"""Explicit provisioning of a NEW synthetic database, never application startup."""

from pathlib import Path
import re
import secrets
from datetime import UTC, datetime

from sqlalchemy import text

from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.models import TenantRow
from attention_router.infrastructure.client_bootstrap_models import (
    ClientDeviceRow,
    ClientTenantMembershipRow,
)
from attention_router.infrastructure.client_session_models import (
    ClientSessionChallengeRow,
    ClientSessionRow,
)

IDENTITY_TABLES = [
    HumanIdentityRow.__table__,
    TenantRow.__table__,
    ClientDeviceRow.__table__,
    ClientTenantMembershipRow.__table__,
    ClientSessionChallengeRow.__table__,
    ClientSessionRow.__table__,
]


def create_disposable_schema(engine):
    """No migrations or global create_all. Only a fresh, dedicated test database."""
    name = engine.url.database or ""
    if not re.fullmatch(r"andy_memory_m1_[0-9a-f]{32}", name):
        raise ValueError("EXCLUSIVE_DISPOSABLE_DATABASE_REQUIRED")
    with engine.begin() as connection:
        if connection.execute(
            text("SELECT count(*) FROM pg_tables WHERE schemaname='public'")
        ).scalar_one():
            raise ValueError("EMPTY_DATABASE_REQUIRED")
        for table in IDENTITY_TABLES:
            table.create(connection)
        source = Path(__file__).resolve().parents[1] / "schema.sql"
        # V0 schema verbatim, except role grants belonging to its separate bench.
        connection.exec_driver_sql(source.read_text().split("GRANT CONNECT")[0])
        connection.exec_driver_sql(Path(__file__).with_name("scope.sql").read_text())
        for table in [t.name for t in IDENTITY_TABLES] + [
            "evidence",
            "information",
            "revision",
            "memory_scopes",
            "memory_scope_information",
        ]:
            # Constant audited table list, never request input. Runtime roles cannot disable triggers.
            connection.exec_driver_sql(
                f"CREATE TRIGGER memory_m1_write_fence BEFORE INSERT OR UPDATE OR DELETE "
                f"ON {table} FOR EACH STATEMENT EXECUTE FUNCTION memory_m1_write_fence()"
            )


def grant_runtime_roles(connection, issuer_role, reader_role):
    """Roles must already exist; no credential generation, grants or DDL at startup."""
    for role in (issuer_role, reader_role):
        if not re.fullmatch(r"m1_[a-z0-9_]{1,48}", role):
            raise ValueError("EXPERIMENTAL_ROLE_REQUIRED")
    connection.exec_driver_sql("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    connection.exec_driver_sql(
        f"GRANT USAGE ON SCHEMA public TO {issuer_role}, {reader_role}"
    )
    names = ",".join(t.name for t in IDENTITY_TABLES)
    connection.exec_driver_sql(
        f"GRANT SELECT ON {names},memory_environment TO {issuer_role}"
    )
    connection.exec_driver_sql(
        f"GRANT INSERT,UPDATE ON client_session_challenges,client_sessions TO {issuer_role}"
    )
    # Existing session issuance locks these rows FOR UPDATE, requiring UPDATE
    # on at least one column. No endpoint exposes a mutation of these IDs.
    for name in ("client_devices", "client_tenant_memberships", "tenants"):
        connection.exec_driver_sql(f"GRANT UPDATE(id) ON {name} TO {issuer_role}")
    # SELECT is sufficient for advisory shared locks. No UPDATE privilege on memory is needed.
    connection.exec_driver_sql(
        f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {reader_role}"
    )


def register_scope(session, *, owner_id, tenant_id, information_ids, expires_at):
    """Administrative provisioning only, in a caller-owned transaction. No HTTP route."""
    if not information_ids or expires_at <= datetime.now(UTC):
        raise ValueError("VALID_SCOPE_REQUIRED")
    owner = session.execute(
        text("""SELECT m.id FROM client_tenant_memberships m
        JOIN tenants t ON t.id=m.tenant_id WHERE m.human_identity_id=:owner
        AND m.tenant_id=:tenant AND m.role='OWNER' AND m.status='ACTIVE'
        AND t.status='ACTIVE'"""),
        {"owner": owner_id, "tenant": tenant_id},
    ).scalar_one_or_none()
    if owner is None:
        raise ValueError("CURRENT_OWNER_REQUIRED")
    scope = "ctx_" + secrets.token_hex(32)
    session.execute(
        text("""INSERT INTO memory_scopes(id,tenant_id,owner_id,purpose,expires_at)
        VALUES(:id,:tenant,:owner,'SYNTHETIC_MEMORY_QUERY',:expires)"""),
        {"id": scope, "tenant": tenant_id, "owner": owner_id, "expires": expires_at},
    )
    for information_id in set(information_ids):
        session.execute(
            text("INSERT INTO memory_scope_information VALUES(:scope,:information)"),
            {"scope": scope, "information": information_id},
        )
    return scope
