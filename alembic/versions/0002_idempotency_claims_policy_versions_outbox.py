"""idempotency claims policy versions outbox

Revision ID: 0002_core_hardening
Revises: 0001_initial
Create Date: 2026-08-07
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_core_hardening"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def json_type():
    return postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.add_column("policies", sa.Column("current_version_id", sa.String(64)))
    op.add_column("policies", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))

    op.create_table(
        "policy_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("policy_id", sa.String(80), sa.ForeignKey("policies.identifier"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config", json_type(), nullable=False),
        sa.Column("checksum", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(120), nullable=False),
        sa.Column("is_immutable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("policy_id", "version", name="uq_policy_versions_policy_version"),
        sa.UniqueConstraint("policy_id", "checksum", name="uq_policy_versions_policy_checksum"),
    )

    op.add_column("interactions", sa.Column("policy_version_id", sa.String(64)))
    op.add_column("interactions", sa.Column("correlation_id", sa.String(64)))
    op.add_column("interactions", sa.Column("causation_id", sa.String(64)))
    op.create_index("ix_interactions_correlation_id", "interactions", ["correlation_id"])

    op.add_column("decisions", sa.Column("policy_version_id", sa.String(64)))
    op.add_column("decisions", sa.Column("correlation_id", sa.String(64)))
    op.add_column("decisions", sa.Column("causation_id", sa.String(64)))
    op.create_index("ix_decisions_correlation_id", "decisions", ["correlation_id"])

    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("interaction_id", sa.String(64), sa.ForeignKey("interactions.id"), nullable=False),
        sa.Column("action_type", sa.String(80), nullable=False),
        sa.Column("destination", sa.String(120), nullable=False),
        sa.Column("payload", json_type(), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_by", sa.String(120)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("idempotency_key", sa.String(180), nullable=False),
        sa.Column("correlation_id", sa.String(64)),
        sa.Column("causation_id", sa.String(64)),
        sa.UniqueConstraint("idempotency_key", name="uq_outbox_idempotency_key"),
    )
    op.create_index("ix_outbox_messages_correlation_id", "outbox_messages", ["correlation_id"])

    op.add_column("action_attempts", sa.Column("outbox_message_id", sa.String(64)))

    for col in [
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_by", sa.String(120)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("correlation_id", sa.String(64)),
        sa.Column("causation_id", sa.String(64)),
    ]:
        op.add_column("timers", col)
    op.create_index("ix_timers_correlation_id", "timers", ["correlation_id"])

    for col in [
        sa.Column("correlation_id", sa.String(64)),
        sa.Column("causation_id", sa.String(64)),
        sa.Column("previous_state", sa.String(80)),
        sa.Column("next_state", sa.String(80)),
        sa.Column("policy_version_id", sa.String(64)),
        sa.Column("origin", sa.String(120), nullable=False, server_default="core"),
    ]:
        op.add_column("audit_events", col)
    op.create_index("ix_audit_events_correlation_id", "audit_events", ["correlation_id"])

    op.create_table(
        "inbound_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("source", sa.String(120), nullable=False),
        sa.Column("external_event_id", sa.String(180), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("payload", json_type(), nullable=False),
        sa.Column("payload_hash", sa.String(128), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("interaction_id", sa.String(64), sa.ForeignKey("interactions.id")),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.UniqueConstraint("source", "external_event_id", name="uq_inbound_source_external"),
    )
    op.create_index("ix_inbound_events_correlation_id", "inbound_events", ["correlation_id"])
    if dialect != "sqlite":
        op.create_foreign_key("fk_interactions_policy_version", "interactions", "policy_versions", ["policy_version_id"], ["id"])
        op.create_foreign_key("fk_decisions_policy_version", "decisions", "policy_versions", ["policy_version_id"], ["id"])
        op.create_foreign_key("fk_action_attempts_outbox", "action_attempts", "outbox_messages", ["outbox_message_id"], ["id"])
        op.create_foreign_key("fk_audit_policy_version", "audit_events", "policy_versions", ["policy_version_id"], ["id"])


def downgrade() -> None:
    op.drop_table("inbound_events")
    op.drop_column("audit_events", "origin")
    op.drop_column("audit_events", "policy_version_id")
    op.drop_column("audit_events", "next_state")
    op.drop_column("audit_events", "previous_state")
    op.drop_column("audit_events", "causation_id")
    op.drop_column("audit_events", "correlation_id")
    for col in ["causation_id", "correlation_id", "completed_at", "last_error", "next_attempt_at", "attempt_count", "claimed_by", "claimed_at"]:
        op.drop_column("timers", col)
    op.drop_column("action_attempts", "outbox_message_id")
    op.drop_table("outbox_messages")
    for table in ["decisions", "interactions"]:
        op.drop_column(table, "causation_id")
        op.drop_column(table, "correlation_id")
        op.drop_column(table, "policy_version_id")
    op.drop_table("policy_versions")
    op.drop_column("policies", "is_active")
    op.drop_column("policies", "current_version_id")
