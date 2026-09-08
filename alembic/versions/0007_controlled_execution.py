"""controlled execution release and outbox relation

Revision ID: 0007_controlled_execution
Revises: 0006_response_review
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0007_controlled_execution"
down_revision: str | None = "0006_response_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_execution_intents", sa.Column("release_status", sa.String(40), nullable=True))
    op.add_column("agent_execution_intents", sa.Column("released_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("agent_execution_intents", sa.Column("released_by", sa.String(120), nullable=True))
    op.add_column("agent_execution_intents", sa.Column("recipient_reference", sa.String(180), nullable=True))
    op.execute("UPDATE agent_execution_intents SET release_status = 'HELD' WHERE release_status IS NULL")
    op.alter_column("agent_execution_intents", "release_status", nullable=False)
    op.create_index("ix_agent_execution_intents_release_status", "agent_execution_intents", ["release_status"])
    op.add_column("outbox_messages", sa.Column("execution_intent_id", sa.String(64), nullable=True))
    op.create_foreign_key(
        "fk_outbox_messages_execution_intent",
        "outbox_messages",
        "agent_execution_intents",
        ["execution_intent_id"],
        ["id"],
    )
    op.create_unique_constraint("uq_outbox_messages_execution_intent", "outbox_messages", ["execution_intent_id"])


def downgrade() -> None:
    op.drop_constraint("uq_outbox_messages_execution_intent", "outbox_messages", type_="unique")
    op.drop_constraint("fk_outbox_messages_execution_intent", "outbox_messages", type_="foreignkey")
    op.drop_column("outbox_messages", "execution_intent_id")
    op.drop_index("ix_agent_execution_intents_release_status", table_name="agent_execution_intents")
    op.drop_column("agent_execution_intents", "recipient_reference")
    op.drop_column("agent_execution_intents", "released_by")
    op.drop_column("agent_execution_intents", "released_at")
    op.drop_column("agent_execution_intents", "release_status")
