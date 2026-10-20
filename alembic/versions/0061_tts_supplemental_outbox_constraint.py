"""Permit one bound supplemental voice effect while retaining one primary effect.

Revision ID: 0061_tts_outbox_constraint
Revises: 0060_first_contact_v1
"""

from alembic import op
import sqlalchemy as sa


revision = "0061_tts_outbox_constraint"
down_revision = "0060_first_contact_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbox_messages", sa.Column("supplemental_parent_outbox_id", sa.String(64)))
    op.create_unique_constraint("uq_outbox_id_intent", "outbox_messages", ["id", "execution_intent_id"])
    op.create_foreign_key(
        "fk_outbox_supplemental_parent_intent", "outbox_messages", "outbox_messages",
        ["supplemental_parent_outbox_id", "execution_intent_id"], ["id", "execution_intent_id"],
    )
    op.create_check_constraint(
        "ck_outbox_supplemental_voice", "outbox_messages",
        "supplemental_parent_outbox_id IS NULL OR ("
        "execution_intent_id IS NOT NULL AND action_type = 'agent_execution_voice' "
        "AND destination = 'local_transport' "
        "AND idempotency_key = 'execution:' || execution_intent_id || ':' || 'audio')",
    )
    op.create_index(
        "uq_outbox_primary_intent", "outbox_messages", ["execution_intent_id"], unique=True,
        postgresql_where=sa.text("execution_intent_id IS NOT NULL AND supplemental_parent_outbox_id IS NULL"),
    )
    op.create_index(
        "uq_outbox_supplemental_intent", "outbox_messages", ["execution_intent_id"], unique=True,
        postgresql_where=sa.text("supplemental_parent_outbox_id IS NOT NULL"),
    )
    op.drop_constraint("uq_outbox_messages_execution_intent", "outbox_messages", type_="unique")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text(
        "SELECT 1 FROM outbox_messages WHERE supplemental_parent_outbox_id IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError("TTS_SUPPLEMENTAL_DOWNGRADE_REQUIRES_EXPLICIT_RECONCILIATION")
    op.create_unique_constraint("uq_outbox_messages_execution_intent", "outbox_messages", ["execution_intent_id"])
    op.drop_index("uq_outbox_supplemental_intent", table_name="outbox_messages")
    op.drop_index("uq_outbox_primary_intent", table_name="outbox_messages")
    op.drop_constraint("ck_outbox_supplemental_voice", "outbox_messages", type_="check")
    op.drop_constraint("fk_outbox_supplemental_parent_intent", "outbox_messages", type_="foreignkey")
    op.drop_constraint("uq_outbox_id_intent", "outbox_messages", type_="unique")
    op.drop_column("outbox_messages", "supplemental_parent_outbox_id")
