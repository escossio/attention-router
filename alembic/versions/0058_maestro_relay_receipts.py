"""MAESTRO relay transport receipt table, no ticket logic.

Revision ID: 0058_maestro_relay_receipts
Revises: 0057_bootstrap_selection
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0058_maestro_relay_receipts"
down_revision = "0057_bootstrap_selection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maestro_relay_receipts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("binding_id", sa.String(64), nullable=False),
        sa.Column("credential_id", sa.String(64), nullable=False),
        sa.Column("source_system", sa.String(120), nullable=False),
        sa.Column("instance_id", sa.String(120), nullable=False),
        sa.Column("occurrence_number", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.String(160), nullable=False),
        sa.Column("signal_status", sa.String(16), nullable=False),
        sa.Column("problem_event_id", sa.String(500)),
        sa.Column("signal_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "binding_id"],
            ["integration_bindings.tenant_id", "integration_bindings.id"],
        ),
        sa.ForeignKeyConstraint(
            ["credential_id", "binding_id"],
            ["integration_credentials.id", "integration_credentials.binding_id"],
        ),
        sa.UniqueConstraint(
            "tenant_id", "source_system", "instance_id", "occurrence_number",
            name="uq_maestro_relay_scope_number",
        ),
        sa.CheckConstraint("occurrence_number > 0", name="ck_maestro_relay_number_positive"),
        sa.CheckConstraint(
            "signal_status in ('PROBLEM','RECOVERY','UPDATE')",
            name="ck_maestro_relay_status",
        ),
    )
    op.create_index(
        "ix_maestro_relay_binding_time",
        "maestro_relay_receipts",
        ["tenant_id", "binding_id", "admitted_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM maestro_relay_receipts LIMIT 1")).first():
        raise RuntimeError("MAESTRO_RELAY_DOWNGRADE_REQUIRES_DATA_EXPORT")
    op.drop_index("ix_maestro_relay_binding_time", table_name="maestro_relay_receipts")
    op.drop_table("maestro_relay_receipts")
