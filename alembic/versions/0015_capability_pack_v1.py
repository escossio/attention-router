"""Capability Pack V1 internal domains.

Revision ID: 0015_capability_pack_v1
Revises: 0014_platform_matrix_v1
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0015_capability_pack_v1"
down_revision = "0014_platform_matrix_v1"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    j = _json_type()
    op.add_column("capability_grants", sa.Column("revoked_by", sa.String(120)))
    op.add_column("capability_grants", sa.Column("revocation_reason", sa.String(240)))
    op.create_table(
        "commitments",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("created_by_actor_id", sa.String(120), nullable=False),
        sa.Column("responsible_actor_id", sa.String(120), nullable=False),
        sa.Column("beneficiary_actor_id", sa.String(120)),
        sa.Column("resource_id", sa.String(64), sa.ForeignKey("resources.id")),
        sa.Column("summary", sa.String(320), nullable=False),
        sa.Column("details", sa.Text()),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("source_event_id", sa.String(64), sa.ForeignKey("canonical_events.id")),
        sa.Column("metadata", j, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_commitments_tenant_responsible_status", "commitments", ["tenant_id", "responsible_actor_id", "status"])
    op.create_index("ix_commitments_tenant_beneficiary_status", "commitments", ["tenant_id", "beneficiary_actor_id", "status"])
    op.create_table(
        "reminders",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("owner_actor_id", sa.String(120), nullable=False),
        sa.Column("resource_id", sa.String(64), sa.ForeignKey("resources.id")),
        sa.Column("summary", sa.String(320), nullable=False),
        sa.Column("trigger_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("source_event_id", sa.String(64), sa.ForeignKey("canonical_events.id")),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("claim_token", sa.String(64)),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_reminder_tenant_idempotency"),
    )
    op.create_index("ix_reminders_due_claim", "reminders", ["status", "trigger_at", "claimed_at"])
    op.create_index("ix_reminders_tenant_owner_status", "reminders", ["tenant_id", "owner_actor_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_reminders_tenant_owner_status", table_name="reminders")
    op.drop_index("ix_reminders_due_claim", table_name="reminders")
    op.drop_table("reminders")
    op.drop_index("ix_commitments_tenant_beneficiary_status", table_name="commitments")
    op.drop_index("ix_commitments_tenant_responsible_status", table_name="commitments")
    op.drop_table("commitments")
    op.drop_column("capability_grants", "revocation_reason")
    op.drop_column("capability_grants", "revoked_by")
