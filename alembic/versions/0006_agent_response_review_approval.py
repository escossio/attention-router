"""agent response review and blocked execution intents

Revision ID: 0006_response_review
Revises: 0005_agent_decisions
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0006_response_review"
down_revision: str | None = "0005_agent_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_response_reviews",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("agent_decision_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("proposed_response_snapshot", sa.Text(), nullable=False),
        sa.Column("edited_response", sa.Text(), nullable=True),
        sa.Column("effective_response", sa.Text(), nullable=False),
        sa.Column("reviewer_type", sa.String(80), nullable=False),
        sa.Column("reviewer_reference", sa.String(120), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_decision_id"], ["agent_decisions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_decision_id", name="uq_agent_response_reviews_decision"),
    )
    op.create_index("ix_agent_response_reviews_status", "agent_response_reviews", ["status"])
    op.create_index("ix_agent_response_reviews_created_at", "agent_response_reviews", ["created_at"])
    op.create_table(
        "agent_execution_intents",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("agent_decision_id", sa.String(64), nullable=False),
        sa.Column("response_review_id", sa.String(64), nullable=False),
        sa.Column("intent_type", sa.String(80), nullable=False),
        sa.Column("effective_response_snapshot", sa.Text(), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("execution_allowed", sa.Boolean(), nullable=False),
        sa.Column("external_delivery_allowed", sa.Boolean(), nullable=False),
        sa.Column("blocked_reason", sa.String(160), nullable=True),
        sa.Column("idempotency_key", sa.String(180), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_decision_id"], ["agent_decisions.id"]),
        sa.ForeignKeyConstraint(["response_review_id"], ["agent_response_reviews.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("response_review_id", name="uq_agent_execution_intents_review"),
        sa.UniqueConstraint("idempotency_key", name="uq_agent_execution_intents_idempotency"),
    )
    op.create_index("ix_agent_execution_intents_status", "agent_execution_intents", ["status"])


def downgrade() -> None:
    op.drop_index("ix_agent_execution_intents_status", table_name="agent_execution_intents")
    op.drop_table("agent_execution_intents")
    op.drop_index("ix_agent_response_reviews_created_at", table_name="agent_response_reviews")
    op.drop_index("ix_agent_response_reviews_status", table_name="agent_response_reviews")
    op.drop_table("agent_response_reviews")
