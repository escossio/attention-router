"""Isolated, fail-closed contextual presence for one WhatsApp group.

Revision ID: 0058_group_contextual_presence
Revises: 0057_bootstrap_selection
"""

from alembic import op
import sqlalchemy as sa

revision = "0058_group_contextual_presence"
down_revision = "0057_bootstrap_selection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_contextual_presence",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("group_id", sa.String(120), nullable=False, unique=True),
        sa.Column("mode", sa.String(24), nullable=False),
        sa.Column("observe_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cursor_at", sa.DateTime(timezone=True)),
        sa.Column("cursor_id", sa.String(64)),
        sa.Column("notice_sent_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("mode in ('OFF','OBSERVE','MENTION_ONLY','CONTEXTUAL')", name="ck_group_presence_mode"),
        sa.CheckConstraint("group_id like '%@g.us'", name="ck_group_presence_group_id"),
    )
    op.create_table(
        "group_contextual_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("presence_id", sa.String(64), sa.ForeignKey("group_contextual_presence.id"), nullable=False),
        sa.Column("inbound_event_id", sa.String(64), sa.ForeignKey("inbound_events.id"), nullable=False, unique=True),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("reason_code", sa.String(120), nullable=False),
        sa.Column("proposal_hash", sa.String(128)),
        sa.Column("outbox_message_id", sa.String(64), sa.ForeignKey("outbox_messages.id")),
        sa.Column("decision_latency_ms", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_group_contextual_events_presence_time", "group_contextual_events", ["presence_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_group_contextual_events_presence_time", table_name="group_contextual_events")
    op.drop_table("group_contextual_events")
    op.drop_table("group_contextual_presence")
