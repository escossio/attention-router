"""Durable sensitive disclosure approval requests.

Revision ID: 0052_sensitive_disclosure
Revises: 0051_owner_profile
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0052_sensitive_disclosure"
down_revision = "0051_owner_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "sensitive_disclosure_requests",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "source_interaction_id",
            sa.String(64),
            sa.ForeignKey("interactions.id"),
            nullable=False,
        ),
        sa.Column(
            "source_event_id",
            sa.String(64),
            sa.ForeignKey("inbound_events.id"),
            nullable=False,
        ),
        sa.Column(
            "requester_actor_binding_id",
            sa.String(64),
            sa.ForeignKey("actor_bindings.id"),
            nullable=True,
        ),
        sa.Column("requester_contact_id", sa.String(180), nullable=False),
        sa.Column("requester_display_name", sa.String(160), nullable=True),
        sa.Column("requester_relationship", sa.String(120), nullable=True),
        sa.Column("requester_identity_source", sa.String(32), nullable=False),
        sa.Column("represented_owner_actor_key", sa.String(120), nullable=False),
        sa.Column(
            "represented_owner_human_identity_id",
            sa.String(64),
            sa.ForeignKey("human_identities.id"),
            nullable=False,
        ),
        sa.Column("represented_owner_reference_name", sa.String(160), nullable=True),
        sa.Column("capability", sa.String(120), nullable=False),
        sa.Column("recipient_reference", sa.String(180), nullable=False),
        sa.Column(
            "execution_intent_id",
            sa.String(64),
            sa.ForeignKey("execution_intents.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "authorization_id",
            sa.String(64),
            sa.ForeignKey("human_execution_authorizations.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "response_outbox_id",
            sa.String(64),
            sa.ForeignKey("outbox_messages.id"),
            nullable=True,
            unique=True,
        ),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("result", json_type, nullable=False),
        sa.Column("failure_reason", sa.String(160), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state in ('PENDING_APPROVAL','RESPONSE_PENDING',"
            "'RESPONDED','FAILED','EXPIRED')",
            name="ck_sensitive_disclosure_request_state",
        ),
        sa.CheckConstraint(
            "requester_identity_source in ('BOUND','SELF_REPORTED','MIXED')",
            name="ck_sensitive_disclosure_request_identity_source",
        ),
        sa.UniqueConstraint(
            "source_interaction_id",
            "capability",
            name="uq_sensitive_disclosure_interaction_capability",
        ),
    )
    op.create_index(
        "ix_sensitive_disclosure_pending",
        "sensitive_disclosure_requests",
        ["tenant_id", "state", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sensitive_disclosure_pending",
        table_name="sensitive_disclosure_requests",
    )
    op.drop_table("sensitive_disclosure_requests")
