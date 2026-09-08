"""persist autonomy evaluations and policy-authorized intents

Revision ID: 0008_controlled_autonomy
Revises: 0007_controlled_execution
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0008_controlled_autonomy"
down_revision: str | None = "0007_controlled_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "autonomy_evaluations",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("agent_decision_id", sa.String(64), nullable=False),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("interaction_id", sa.String(64), nullable=False),
        sa.Column("agent_blueprint_id", sa.String(64), nullable=True),
        sa.Column("agent_blueprint_version", sa.Integer(), nullable=True),
        sa.Column("policy_version_id", sa.String(64), nullable=True),
        sa.Column("blueprint_mode", sa.String(40), nullable=False),
        sa.Column("policy_mode", sa.String(40), nullable=False),
        sa.Column("effective_mode", sa.String(40), nullable=False),
        sa.Column("decision_type", sa.String(60), nullable=False),
        sa.Column("recommended_action", sa.String(120), nullable=False),
        sa.Column("action_allowed", sa.Boolean(), nullable=False),
        sa.Column("actor_scope_valid", sa.Boolean(), nullable=False),
        sa.Column("audience_scope_valid", sa.Boolean(), nullable=False),
        sa.Column("freshness_valid", sa.Boolean(), nullable=False),
        sa.Column("from_me", sa.Boolean(), nullable=False),
        sa.Column("automatic_execution_allowed", sa.Boolean(), nullable=False),
        sa.Column("reason_code", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_decision_id"], ["agent_decisions.id"]),
        sa.ForeignKeyConstraint(["event_id"], ["inbound_events.id"]),
        sa.ForeignKeyConstraint(["interaction_id"], ["interactions.id"]),
        sa.ForeignKeyConstraint(["agent_blueprint_id"], ["agent_blueprints.id"]),
        sa.ForeignKeyConstraint(["policy_version_id"], ["policy_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_decision_id", name="uq_autonomy_evaluations_decision"),
    )
    op.create_index("ix_autonomy_evaluations_effective_mode", "autonomy_evaluations", ["effective_mode"])
    op.add_column("agent_execution_intents", sa.Column("autonomy_evaluation_id", sa.String(64), nullable=True))
    op.add_column("agent_execution_intents", sa.Column("authorization_source", sa.String(40), nullable=True))
    op.execute("UPDATE agent_execution_intents SET authorization_source = 'HUMAN_REVIEW' WHERE authorization_source IS NULL")
    op.alter_column("agent_execution_intents", "authorization_source", nullable=False)
    op.alter_column("agent_execution_intents", "response_review_id", nullable=True)
    op.create_foreign_key(
        "fk_agent_execution_intents_autonomy_evaluation",
        "agent_execution_intents",
        "autonomy_evaluations",
        ["autonomy_evaluation_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_agent_execution_intents_autonomy_evaluation",
        "agent_execution_intents",
        ["autonomy_evaluation_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_agent_execution_intents_autonomy_evaluation", "agent_execution_intents", type_="unique")
    op.drop_constraint("fk_agent_execution_intents_autonomy_evaluation", "agent_execution_intents", type_="foreignkey")
    op.alter_column("agent_execution_intents", "response_review_id", nullable=False)
    op.drop_column("agent_execution_intents", "authorization_source")
    op.drop_column("agent_execution_intents", "autonomy_evaluation_id")
    op.drop_index("ix_autonomy_evaluations_effective_mode", table_name="autonomy_evaluations")
    op.drop_table("autonomy_evaluations")
