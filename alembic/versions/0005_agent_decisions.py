"""agent decision dry-run persistence

Revision ID: 0005_agent_decisions
Revises: 0004_agent_builder_v0
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0005_agent_decisions"
down_revision: str | None = "0004_agent_builder_v0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")
    op.create_table(
        "agent_decisions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("interaction_id", sa.String(length=64), nullable=False),
        sa.Column("agent_blueprint_id", sa.String(length=64), nullable=True),
        sa.Column("agent_blueprint_version", sa.Integer(), nullable=True),
        sa.Column("actor_id", sa.String(length=120), nullable=True),
        sa.Column("actor_binding_id", sa.String(length=64), nullable=True),
        sa.Column("audience", sa.String(length=160), nullable=True),
        sa.Column("policy_version_id", sa.String(length=64), nullable=True),
        sa.Column("decision_pipeline_version", sa.String(length=40), nullable=False),
        sa.Column("decision_type", sa.String(length=60), nullable=False),
        sa.Column("recommended_action", sa.String(length=120), nullable=False),
        sa.Column("proposed_response", sa.Text(), nullable=True),
        sa.Column("escalation_required", sa.Boolean(), nullable=False),
        sa.Column("escalation_reason", sa.String(length=240), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("missing_information", json_type, nullable=False),
        sa.Column("execution_allowed", sa.Boolean(), nullable=False),
        sa.Column("external_delivery_allowed", sa.Boolean(), nullable=False),
        sa.Column("reasoning_summary", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["inbound_events.id"]),
        sa.ForeignKeyConstraint(["interaction_id"], ["interactions.id"]),
        sa.ForeignKeyConstraint(["agent_blueprint_id"], ["agent_blueprints.id"]),
        sa.ForeignKeyConstraint(["actor_binding_id"], ["actor_bindings.id"]),
        sa.ForeignKeyConstraint(["policy_version_id"], ["policy_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "decision_pipeline_version", name="uq_agent_decision_event_pipeline"),
    )
    op.create_index("ix_agent_decisions_event_id", "agent_decisions", ["event_id"])
    op.create_index("ix_agent_decisions_interaction_id", "agent_decisions", ["interaction_id"])
    op.create_index("ix_agent_decisions_blueprint_id", "agent_decisions", ["agent_blueprint_id"])
    op.create_index("ix_agent_decisions_decision_type", "agent_decisions", ["decision_type"])
    op.create_index("ix_agent_decisions_created_at", "agent_decisions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_decisions_created_at", table_name="agent_decisions")
    op.drop_index("ix_agent_decisions_decision_type", table_name="agent_decisions")
    op.drop_index("ix_agent_decisions_blueprint_id", table_name="agent_decisions")
    op.drop_index("ix_agent_decisions_interaction_id", table_name="agent_decisions")
    op.drop_index("ix_agent_decisions_event_id", table_name="agent_decisions")
    op.drop_table("agent_decisions")
