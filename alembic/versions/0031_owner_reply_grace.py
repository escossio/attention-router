"""Add durable owner reply grace windows before decision enqueue."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0031_owner_reply_grace"
down_revision = "0030_meta_callback_release_gate"
branch_labels = None
depends_on = None


def json_type():
    return postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "conversation_response_grace_windows",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("source", sa.String(120), nullable=False),
        sa.Column("source_account", sa.String(180), nullable=False),
        sa.Column("channel", sa.String(80), nullable=False),
        sa.Column("conversation_key", sa.String(240), nullable=False),
        sa.Column("actor_id", sa.String(120), nullable=False),
        sa.Column("actor_binding_id", sa.String(64), sa.ForeignKey("actor_bindings.id"), nullable=False),
        sa.Column("audience", sa.String(120), nullable=False),
        sa.Column("policy_version_id", sa.String(64), sa.ForeignKey("policy_versions.id"), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("anchor_event_id", sa.String(64), sa.ForeignKey("inbound_events.id"), nullable=False),
        sa.Column("anchor_interaction_id", sa.String(64), sa.ForeignKey("interactions.id"), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("canceled_at", sa.DateTime(timezone=True)),
        sa.Column("canceled_by_event_id", sa.String(64), sa.ForeignKey("inbound_events.id")),
        sa.Column("cancellation_reason", sa.String(120)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_by", sa.String(120)),
        sa.Column("claimed_generation", sa.Integer()),
        sa.Column("provenance", json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state in ('OPEN','CANCELED','RELEASED','SUPERSEDED')", name="ck_response_grace_state"),
        sa.CheckConstraint("generation > 0", name="ck_response_grace_generation_positive"),
        sa.CheckConstraint("due_at >= last_inbound_at", name="ck_response_grace_due_after_inbound"),
    )
    op.create_index(
        "uq_response_grace_open_conversation",
        "conversation_response_grace_windows",
        ["tenant_id", "source", "source_account", "conversation_key"],
        unique=True,
        postgresql_where=sa.text("state = 'OPEN'"),
    )
    op.create_index(
        "ix_response_grace_due",
        "conversation_response_grace_windows",
        ["due_at", "id"],
        postgresql_where=sa.text("state = 'OPEN'"),
    )
    op.create_table(
        "conversation_response_grace_inbounds",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("grace_window_id", sa.String(64), sa.ForeignKey("conversation_response_grace_windows.id"), nullable=False),
        sa.Column("inbound_event_id", sa.String(64), sa.ForeignKey("inbound_events.id"), nullable=False),
        sa.Column("interaction_id", sa.String(64), sa.ForeignKey("interactions.id"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("associated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("inbound_event_id", name="uq_response_grace_inbound_event"),
        sa.UniqueConstraint("grace_window_id", "interaction_id", name="uq_response_grace_window_interaction"),
        sa.CheckConstraint("generation > 0", name="ck_response_grace_inbound_generation_positive"),
    )
    op.create_index(
        "ix_response_grace_inbounds_window_generation",
        "conversation_response_grace_inbounds",
        ["grace_window_id", "generation"],
    )


def downgrade() -> None:
    op.drop_index("ix_response_grace_inbounds_window_generation", table_name="conversation_response_grace_inbounds")
    op.drop_table("conversation_response_grace_inbounds")
    op.drop_index("ix_response_grace_due", table_name="conversation_response_grace_windows")
    op.drop_index("uq_response_grace_open_conversation", table_name="conversation_response_grace_windows")
    op.drop_table("conversation_response_grace_windows")
