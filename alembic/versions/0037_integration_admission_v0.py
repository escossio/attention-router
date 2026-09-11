"""Persistent neutral bindings and atomic inbox; no dispatcher."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0037_integration_admission_v0"
down_revision = "0036_artifact_registry_v0"
branch_labels = None
depends_on = None


def upgrade():
    j = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "integration_bindings",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("audience", sa.String(120), nullable=False),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("instance_id", sa.String(120), nullable=False),
        sa.Column("account_key", sa.String(180), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("scopes", j, nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_integration_binding_tenant"),
        sa.UniqueConstraint("audience", "tenant_id", "kind", "name", "instance_id", "account_key",
                            name="uq_integration_binding_namespace"),
        sa.CheckConstraint("kind in ('CHANNEL','CAPABILITY')", name="ck_integration_binding_kind"),
    )
    op.create_table(
        "integration_credentials",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False, unique=True),
        sa.Column("binding_id", sa.String(64), sa.ForeignKey("integration_bindings.id"), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("scopes", j, nullable=False),
        sa.UniqueConstraint("id", "binding_id", name="uq_integration_credential_binding"),
        sa.CheckConstraint("expires_at > not_before", name="ck_integration_credential_lifetime"),
    )
    op.create_table(
        "integration_inbox",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("binding_id", sa.String(64), nullable=False),
        sa.Column("credential_id", sa.String(64), nullable=False),
        sa.Column("contract_type", sa.String(32), nullable=False),
        sa.Column("external_event_id", sa.String(240), nullable=False),
        sa.Column("idempotency_key", sa.String(240), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("raw_body", sa.LargeBinary(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "binding_id"],
                                ["integration_bindings.tenant_id", "integration_bindings.id"]),
        sa.ForeignKeyConstraint(["credential_id", "binding_id"],
                                ["integration_credentials.id", "integration_credentials.binding_id"]),
        sa.UniqueConstraint("tenant_id", "binding_id", "contract_type", "external_event_id",
                            name="uq_integration_inbox_event"),
        sa.UniqueConstraint("tenant_id", "binding_id", "contract_type", "idempotency_key",
                            name="uq_integration_inbox_key"),
        sa.CheckConstraint("contract_type = 'inbound_event'", name="ck_integration_inbox_type"),
        sa.CheckConstraint("state = 'PENDING'", name="ck_integration_inbox_state"),
        sa.CheckConstraint("length(raw_body) <= 65536", name="ck_integration_inbox_size"),
    )
    if op.get_bind().dialect.name == "postgresql":
        # IDs cannot be retargeted or deleted to reset a deduplication namespace.
        op.execute("""
        CREATE FUNCTION guard_integration_identity() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'INTEGRATION_IDENTITY_IMMUTABLE';
          END IF;
          IF TG_TABLE_NAME = 'integration_bindings' THEN
            IF (to_jsonb(NEW) - 'active' - 'scopes') IS DISTINCT FROM
               (to_jsonb(OLD) - 'active' - 'scopes') THEN
              RAISE EXCEPTION 'INTEGRATION_IDENTITY_IMMUTABLE';
            END IF;
          ELSIF TG_TABLE_NAME = 'integration_credentials' THEN
            IF (to_jsonb(NEW) - 'revoked' - 'scopes') IS DISTINCT FROM
               (to_jsonb(OLD) - 'revoked' - 'scopes') OR (OLD.revoked AND NOT NEW.revoked) THEN
              RAISE EXCEPTION 'INTEGRATION_IDENTITY_IMMUTABLE';
            END IF;
          ELSE
            RAISE EXCEPTION 'INTEGRATION_INBOX_IMMUTABLE';
          END IF;
          RETURN NEW;
        END $$
        """)
        for table in ("integration_bindings", "integration_credentials", "integration_inbox"):
            op.execute(f"CREATE TRIGGER guard_identity BEFORE UPDATE OR DELETE ON {table} "
                       "FOR EACH ROW EXECUTE FUNCTION guard_integration_identity()")


def downgrade():
    bind = op.get_bind()
    tables = ("integration_inbox", "integration_credentials", "integration_bindings")
    for table in tables:
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("INTEGRATION_ADMISSION_DOWNGRADE_REQUIRES_DATA_EXPORT")
    for table in tables:
        op.drop_table(table)
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION guard_integration_identity()")
