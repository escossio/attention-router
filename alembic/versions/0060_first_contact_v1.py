"""Durable, tenant-scoped operational first-contact presentation receipts.

Revision ID: 0060_first_contact_v1
Revises: 0059_maestro_relay_receipts
"""

from alembic import op
import sqlalchemy as sa


revision = "0060_first_contact_v1"
down_revision = "0059_maestro_relay_receipts"
branch_labels = None
depends_on = None

_STATES = "'RESERVED','QUEUED','SENDING','SENT','CONFIRMED','FAILED','AMBIGUOUS'"


def upgrade() -> None:
    op.create_table(
        "first_contact_presentations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("source", sa.String(120), nullable=False),
        sa.Column("channel", sa.String(40), nullable=False),
        sa.Column("source_account", sa.String(180), nullable=False),
        sa.Column("canonical_peer_hash", sa.String(64), nullable=False),
        sa.Column("presentation_version", sa.String(80), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("current_attempt_id", sa.String(64)),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "source", "channel", "source_account", "canonical_peer_hash",
                            name="uq_first_contact_scope"),
        sa.CheckConstraint(f"state IN ({_STATES})", name="ck_first_contact_state"),
        sa.CheckConstraint("generation > 0 AND revision >= 0", name="ck_first_contact_revision"),
        sa.CheckConstraint("length(canonical_peer_hash) = 64", name="ck_first_contact_peer_hash"),
        sa.CheckConstraint("(state = 'CONFIRMED') = (confirmed_at IS NOT NULL)",
                           name="ck_first_contact_confirmation"),
    )
    op.create_index("ix_first_contact_state", "first_contact_presentations", ["tenant_id", "state"])
    op.create_table(
        "first_contact_attempts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("presentation_id", sa.String(64), sa.ForeignKey("first_contact_presentations.id"), nullable=False),
        sa.Column("event_id", sa.String(64), sa.ForeignKey("inbound_events.id"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("presentation_version", sa.String(80), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("decision_id", sa.String(64), sa.ForeignKey("agent_decisions.id")),
        sa.Column("execution_intent_id", sa.String(64), sa.ForeignKey("agent_execution_intents.id")),
        sa.Column("outbox_id", sa.String(64), sa.ForeignKey("outbox_messages.id")),
        sa.Column("idempotency_key", sa.String(180)),
        sa.Column("text_hash", sa.String(64)),
        sa.Column("message_reference", sa.String(512)),
        sa.Column("ack", sa.Integer()),
        sa.Column("ack_at", sa.DateTime(timezone=True)),
        sa.Column("proof_sha256", sa.String(64)),
        sa.Column("last_probed_at", sa.DateTime(timezone=True)),
        sa.Column("failure_reason", sa.String(160)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True)),
        sa.Column("queued_at", sa.DateTime(timezone=True)),
        sa.Column("sending_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("presentation_id", "id", name="uq_first_contact_attempt_scope_id"),
        sa.UniqueConstraint("presentation_id", "generation", name="uq_first_contact_generation"),
        sa.UniqueConstraint("event_id", name="uq_first_contact_attempt_event"),
        sa.UniqueConstraint("decision_id", name="uq_first_contact_attempt_decision"),
        sa.UniqueConstraint("execution_intent_id", name="uq_first_contact_attempt_intent"),
        sa.UniqueConstraint("outbox_id", name="uq_first_contact_attempt_outbox"),
        sa.CheckConstraint(f"state IN ({_STATES})", name="ck_first_contact_attempt_state"),
        sa.CheckConstraint("generation > 0", name="ck_first_contact_attempt_generation"),
        sa.CheckConstraint("text_hash IS NULL OR length(text_hash) = 64", name="ck_first_contact_text_hash"),
        sa.CheckConstraint("ack IS NULL OR ack BETWEEN 0 AND 4", name="ck_first_contact_ack"),
        sa.CheckConstraint(
            "state != 'CONFIRMED' OR (confirmed_at IS NOT NULL AND ack IS NOT NULL AND ack >= 2 "
            "AND ack_at IS NOT NULL AND decision_id IS NOT NULL AND execution_intent_id IS NOT NULL "
            "AND idempotency_key IS NOT NULL "
            "AND proof_sha256 IS NOT NULL AND message_reference IS NOT NULL "
            "AND outbox_id IS NOT NULL AND text_hash IS NOT NULL)",
            name="ck_first_contact_attempt_confirmation",
        ),
    )
    op.create_foreign_key(
        "fk_first_contact_current_attempt_scope", "first_contact_presentations", "first_contact_attempts",
        ["id", "current_attempt_id"], ["presentation_id", "id"],
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM first_contact_presentations LIMIT 1")).first():
        raise RuntimeError("FIRST_CONTACT_DOWNGRADE_REQUIRES_DATA_EXPORT")
    op.drop_constraint("fk_first_contact_current_attempt_scope", "first_contact_presentations", type_="foreignkey")
    op.drop_table("first_contact_attempts")
    op.drop_index("ix_first_contact_state", table_name="first_contact_presentations")
    op.drop_table("first_contact_presentations")
