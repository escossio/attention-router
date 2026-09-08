"""add persistent isolated laboratory conversation sessions

Revision ID: 0012_lab_conversation
Revises: 0011_decision_contract
"""

import sqlalchemy as sa
from alembic import op

revision = "0012_lab_conversation"
down_revision = "0011_decision_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_decisions", sa.Column("response_source", sa.String(80), nullable=True))
    op.create_table(
        "lab_conversation_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("target_binding_id", sa.String(64), sa.ForeignKey("actor_bindings.id"), nullable=False),
        sa.Column("target_policy", sa.String(80), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("max_inbounds", sa.Integer(), nullable=False),
        sa.Column("max_send_permits", sa.Integer(), nullable=False),
        sa.Column("inbound_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("send_permit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_slot", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(120), nullable=False),
    )
    op.create_index("uq_lab_active_binding", "lab_conversation_sessions", ["target_binding_id"], unique=True, postgresql_where=sa.text("status = 'ACTIVE'"), sqlite_where=sa.text("status = 'ACTIVE'"))
    op.create_table(
        "lab_conversation_inbounds",
        sa.Column("session_id", sa.String(64), sa.ForeignKey("lab_conversation_sessions.id"), primary_key=True),
        sa.Column("inbound_event_id", sa.String(64), sa.ForeignKey("inbound_events.id"), unique=True, nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "lab_conversation_delivery_permits",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("lab_conversation_sessions.id"), nullable=False),
        sa.Column("review_id", sa.String(64), sa.ForeignKey("agent_response_reviews.id"), unique=True, nullable=False),
        sa.Column("target_binding_id", sa.String(64), sa.ForeignKey("actor_bindings.id"), nullable=False),
        sa.Column("permit_ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_execution_intent_id", sa.String(64), sa.ForeignKey("agent_execution_intents.id")),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_index("uq_lab_active_binding", table_name="lab_conversation_sessions")
    op.drop_table("lab_conversation_delivery_permits")
    op.drop_table("lab_conversation_inbounds")
    op.drop_table("lab_conversation_sessions")
    op.drop_column("agent_decisions", "response_source")
