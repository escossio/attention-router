"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-07
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def json_type():
    return postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "policies",
        sa.Column("identifier", sa.String(80), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("match_criteria", json_type(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("specificity", sa.Integer(), nullable=False),
        sa.Column("tone", sa.String(80), nullable=False),
        sa.Column("initial_wait_seconds", sa.Integer(), nullable=False),
        sa.Column("allowed_disclosures", json_type(), nullable=False),
        sa.Column("allowed_actions", json_type(), nullable=False),
        sa.Column("escalation_steps", json_type(), nullable=False),
        sa.Column("ack_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("repetition_limit", sa.Integer(), nullable=False),
        sa.Column("cancellation_conditions", json_type(), nullable=False),
        sa.Column("completion_conditions", json_type(), nullable=False),
    )
    op.create_table(
        "interactions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("contact_id", sa.String(120), nullable=False),
        sa.Column("contact_name", sa.String(160), nullable=False),
        sa.Column("relationship_category", sa.String(120), nullable=False),
        sa.Column("active_context", sa.String(120)),
        sa.Column("inbound_text", sa.Text(), nullable=False),
        sa.Column("state", sa.String(80), nullable=False),
        sa.Column("policy_id", sa.String(80), sa.ForeignKey("policies.identifier")),
        sa.Column("lia_speech", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "decisions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("interaction_id", sa.String(64), sa.ForeignKey("interactions.id"), nullable=False),
        sa.Column("policy_id", sa.String(80), sa.ForeignKey("policies.identifier"), nullable=False),
        sa.Column("matched_rules", json_type(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "action_attempts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("interaction_id", sa.String(64), sa.ForeignKey("interactions.id"), nullable=False),
        sa.Column("action_key", sa.String(80), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "timers",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("interaction_id", sa.String(64), sa.ForeignKey("interactions.id"), nullable=False),
        sa.Column("action_attempt_id", sa.String(64), sa.ForeignKey("action_attempts.id")),
        sa.Column("kind", sa.String(80), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "acknowledgements",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("interaction_id", sa.String(64), sa.ForeignKey("interactions.id"), nullable=False),
        sa.Column("action_attempt_id", sa.String(64), sa.ForeignKey("action_attempts.id"), nullable=False),
        sa.Column("source", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("interaction_id", sa.String(64), sa.ForeignKey("interactions.id")),
        sa.Column("event_type", sa.String(120), nullable=False),
        sa.Column("payload", json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "queue",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(80), nullable=False),
        sa.Column("payload", json_type(), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    for table in [
        "queue",
        "audit_events",
        "acknowledgements",
        "timers",
        "action_attempts",
        "decisions",
        "interactions",
        "policies",
    ]:
        op.drop_table(table)

