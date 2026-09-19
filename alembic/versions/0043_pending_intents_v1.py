"""Add Pending Intent V1 persistence boundary."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0043_pending_intents_v1"
down_revision = "0042_client_location_snapshot"
branch_labels = None
depends_on = None


def json_type():
    return postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "pending_intents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("represented_owner_actor_key", sa.String(120), nullable=False),
        sa.Column(
            "source_inbound_event_id",
            sa.String(64),
            sa.ForeignKey("inbound_events.id"),
            nullable=False,
        ),
        sa.Column(
            "source_interaction_id",
            sa.String(64),
            sa.ForeignKey("interactions.id"),
            nullable=False,
        ),
        sa.Column("source_channel", sa.String(80), nullable=False),
        sa.Column("conversation_key_hash", sa.String(64), nullable=False),
        sa.Column("semantic_registry_version", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("ambiguity_reason", sa.String(120), nullable=False),
        sa.Column("candidate_set", json_type(), nullable=False),
        sa.Column("candidate_set_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "clarification_outbox_id",
            sa.String(64),
            sa.ForeignKey("outbox_messages.id"),
        ),
        sa.Column("clarification_delivered_at", sa.DateTime(timezone=True)),
        sa.Column(
            "resolution_inbound_event_id",
            sa.String(64),
            sa.ForeignKey("inbound_events.id"),
        ),
        sa.Column("selected_candidate_key", sa.String(64)),
        sa.Column("resolution_kind", sa.String(40)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("provenance", json_type(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_inbound_event_id",
            name="uq_pending_intent_source_event",
        ),
        sa.CheckConstraint(
            "state in ('PENDING','RESOLVED','CANCELED','SUPERSEDED','EXPIRED')",
            name="ck_pending_intent_state",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_pending_intent_validity",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_pending_intent_version_positive",
        ),
        sa.CheckConstraint(
            "selected_candidate_key is null or state = 'RESOLVED'",
            name="ck_pending_intent_selected_only_when_resolved",
        ),
        sa.CheckConstraint(
            "state != 'RESOLVED' or "
            "(selected_candidate_key is not null and "
            "resolution_inbound_event_id is not null and "
            "resolution_kind is not null and resolved_at is not null)",
            name="ck_pending_intent_resolved_complete",
        ),
        sa.CheckConstraint(
            "clarification_delivered_at is null or clarification_outbox_id is not null",
            name="ck_pending_intent_delivery_requires_outbox",
        ),
    )
    op.create_index(
        "uq_pending_intent_clarification_outbox",
        "pending_intents",
        ["clarification_outbox_id"],
        unique=True,
        postgresql_where=sa.text("clarification_outbox_id IS NOT NULL"),
        sqlite_where=sa.text("clarification_outbox_id IS NOT NULL"),
    )
    op.create_index(
        "uq_pending_intent_resolution_event",
        "pending_intents",
        ["resolution_inbound_event_id"],
        unique=True,
        postgresql_where=sa.text("resolution_inbound_event_id IS NOT NULL"),
        sqlite_where=sa.text("resolution_inbound_event_id IS NOT NULL"),
    )
    op.create_index(
        "uq_pending_intent_active_scope",
        "pending_intents",
        [
            "tenant_id",
            "represented_owner_actor_key",
            "source_channel",
            "conversation_key_hash",
        ],
        unique=True,
        postgresql_where=sa.text("state = 'PENDING'"),
        sqlite_where=sa.text("state = 'PENDING'"),
    )
    op.create_index(
        "ix_pending_intent_lookup",
        "pending_intents",
        [
            "tenant_id",
            "represented_owner_actor_key",
            "source_channel",
            "conversation_key_hash",
            "state",
            "expires_at",
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_pending_intent_lookup", table_name="pending_intents")
    op.drop_index("uq_pending_intent_active_scope", table_name="pending_intents")
    op.drop_index("uq_pending_intent_resolution_event", table_name="pending_intents")
    op.drop_index("uq_pending_intent_clarification_outbox", table_name="pending_intents")
    op.drop_table("pending_intents")
