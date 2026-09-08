"""Global tenant/represented-owner automation switch, independent of policy Grace."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0033_owner_automation_controls"
down_revision = "0032_owner_operational_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
    op.create_table(
        "owner_automation_controls",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("represented_owner_actor_key", sa.String(120), nullable=False),
        sa.Column("automatic_responses_enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_by_actor_key", sa.String(120), nullable=False),
        sa.Column("authorization_source", sa.String(80), nullable=False),
        sa.Column("source_channel", sa.String(80), nullable=False),
        sa.Column("source_event_id", sa.String(180), nullable=False),
        sa.Column("provenance", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "represented_owner_actor_key", name="uq_owner_automation_scope"
        ),
        sa.CheckConstraint("revision >= 0", name="ck_owner_automation_revision"),
    )
    op.create_table(
        "owner_automation_control_changes",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "control_id",
            sa.String(64),
            sa.ForeignKey("owner_automation_controls.id"),
            nullable=False,
        ),
        sa.Column("represented_owner_actor_key", sa.String(120), nullable=False),
        sa.Column("source_channel", sa.String(80), nullable=False),
        sa.Column("source_event_id", sa.String(180), nullable=False),
        sa.Column("requested_enabled", sa.Boolean(), nullable=False),
        sa.Column("previous_revision", sa.Integer(), nullable=False),
        sa.Column("resulting_revision", sa.Integer(), nullable=False),
        sa.Column("changed", sa.Boolean(), nullable=False),
        sa.Column("provenance", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "source_channel",
            "source_event_id",
            name="uq_owner_automation_change_event",
        ),
        sa.CheckConstraint(
            "previous_revision >= 0 and resulting_revision >= previous_revision",
            name="ck_owner_automation_change_revisions",
        ),
    )
    op.create_index(
        "ix_owner_automation_changes_control",
        "owner_automation_control_changes",
        ["control_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_owner_automation_changes_control", table_name="owner_automation_control_changes"
    )
    op.drop_table("owner_automation_control_changes")
    op.drop_table("owner_automation_controls")
