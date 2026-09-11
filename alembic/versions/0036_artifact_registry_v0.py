"""Tenant-scoped canonical artifact registry and source receipts."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0036_artifact_registry_v0"
down_revision = "0035_whatsapp_voice_media"
branch_labels = None
depends_on = None


def upgrade():
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "resource_id",
            sa.String(64),
            sa.ForeignKey("resources.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("artifact_kind", sa.String(40), nullable=False),
        sa.Column("mime_type", sa.String(160), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_provider", sa.String(80), nullable=False),
        sa.Column("storage_reference", sa.String(512), nullable=False),
        sa.Column("original_filename", sa.String(512)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("metadata", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "content_sha256", name="uq_artifact_tenant_sha256"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_artifact_tenant_id"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_artifact_size_nonnegative"),
        sa.CheckConstraint(
            "status in ('AVAILABLE','FAILED','DELETED')",
            name="ck_artifact_status",
        ),
    )
    op.create_index(
        "ix_artifacts_tenant_created",
        "artifacts",
        ["tenant_id", "created_at"],
    )

    op.create_table(
        "artifact_receipts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("artifact_id", sa.String(64), nullable=False),
        sa.Column("source_channel", sa.String(80), nullable=False),
        sa.Column("source_account", sa.String(120), nullable=False),
        sa.Column("external_receipt_id", sa.String(240), nullable=False),
        sa.Column("sender_actor_id", sa.String(120)),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["artifacts.tenant_id", "artifacts.id"],
            name="fk_artifact_receipt_tenant_artifact",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "source_channel",
            "source_account",
            "external_receipt_id",
            name="uq_artifact_receipt_source",
        ),
    )
    op.create_index(
        "ix_artifact_receipts_tenant_artifact_received",
        "artifact_receipts",
        ["tenant_id", "artifact_id", "received_at"],
    )
    op.create_index(
        "ix_artifact_receipts_tenant_received",
        "artifact_receipts",
        ["tenant_id", "received_at"],
    )


def downgrade():
    bind = op.get_bind()
    for table in ("artifact_receipts", "artifacts"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("ARTIFACT_REGISTRY_DOWNGRADE_REQUIRES_DATA_EXPORT")

    op.drop_index(
        "ix_artifact_receipts_tenant_received",
        table_name="artifact_receipts",
    )
    op.drop_index(
        "ix_artifact_receipts_tenant_artifact_received",
        table_name="artifact_receipts",
    )
    op.drop_table("artifact_receipts")
    op.drop_index("ix_artifacts_tenant_created", table_name="artifacts")
    op.drop_table("artifacts")
