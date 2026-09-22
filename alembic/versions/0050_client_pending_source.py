"""Allow Pending Intent provenance from authenticated client commands.

Revision ID: 0050_client_pending_source
Revises: 0049_client_command_channel_v1
"""

from alembic import op
import sqlalchemy as sa


revision = "0050_client_pending_source"
down_revision = "0049_client_command_channel_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pending_intents",
        sa.Column(
            "source_tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.id"),
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            "UPDATE pending_intents SET source_tenant_id = tenant_id "
            "WHERE source_tenant_id IS NULL"
        )
    )
    op.alter_column(
        "pending_intents",
        "source_tenant_id",
        existing_type=sa.String(64),
        nullable=False,
    )
    op.add_column(
        "pending_intents",
        sa.Column(
            "source_client_command_id",
            sa.String(64),
            sa.ForeignKey("client_command_messages.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "pending_intents",
        sa.Column(
            "resolution_client_command_id",
            sa.String(64),
            sa.ForeignKey("client_command_messages.id"),
            nullable=True,
        ),
    )
    op.alter_column(
        "pending_intents",
        "source_inbound_event_id",
        existing_type=sa.String(64),
        nullable=True,
    )
    op.alter_column(
        "pending_intents",
        "source_interaction_id",
        existing_type=sa.String(64),
        nullable=True,
    )
    op.drop_constraint(
        "ck_pending_intent_resolved_complete",
        "pending_intents",
        type_="check",
    )
    op.create_unique_constraint(
        "uq_pending_intent_source_client_command",
        "pending_intents",
        ["source_client_command_id"],
    )
    op.create_check_constraint(
        "ck_pending_intent_source_exactly_one",
        "pending_intents",
        "((source_inbound_event_id is not null and "
        "source_interaction_id is not null and source_client_command_id is null) or "
        "(source_inbound_event_id is null and source_interaction_id is null and "
        "source_client_command_id is not null))",
    )
    op.create_check_constraint(
        "ck_pending_intent_resolution_at_most_one",
        "pending_intents",
        "not (resolution_inbound_event_id is not null and "
        "resolution_client_command_id is not null)",
    )
    op.create_check_constraint(
        "ck_pending_intent_resolved_complete",
        "pending_intents",
        "state != 'RESOLVED' or "
        "(selected_candidate_key is not null and "
        "((resolution_inbound_event_id is not null and "
        "resolution_client_command_id is null) or "
        "(resolution_inbound_event_id is null and "
        "resolution_client_command_id is not null)) and "
        "resolution_kind is not null and resolved_at is not null)",
    )
    op.create_index(
        "uq_pending_intent_resolution_client_command",
        "pending_intents",
        ["resolution_client_command_id"],
        unique=True,
        postgresql_where=sa.text("resolution_client_command_id IS NOT NULL"),
        sqlite_where=sa.text("resolution_client_command_id IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            "SELECT 1 FROM pending_intents "
            "WHERE source_client_command_id IS NOT NULL "
            "OR resolution_client_command_id IS NOT NULL LIMIT 1"
        )
    ).first():
        raise RuntimeError(
            "PENDING_INTENT_CLIENT_COMMAND_DOWNGRADE_REQUIRES_DATA_EXPORT"
        )

    op.drop_index(
        "uq_pending_intent_resolution_client_command",
        table_name="pending_intents",
    )
    op.drop_constraint(
        "ck_pending_intent_resolved_complete",
        "pending_intents",
        type_="check",
    )
    op.drop_constraint(
        "ck_pending_intent_resolution_at_most_one",
        "pending_intents",
        type_="check",
    )
    op.drop_constraint(
        "ck_pending_intent_source_exactly_one",
        "pending_intents",
        type_="check",
    )
    op.drop_constraint(
        "uq_pending_intent_source_client_command",
        "pending_intents",
        type_="unique",
    )
    op.alter_column(
        "pending_intents",
        "source_interaction_id",
        existing_type=sa.String(64),
        nullable=False,
    )
    op.alter_column(
        "pending_intents",
        "source_inbound_event_id",
        existing_type=sa.String(64),
        nullable=False,
    )
    op.drop_column("pending_intents", "resolution_client_command_id")
    op.drop_column("pending_intents", "source_client_command_id")
    op.drop_column("pending_intents", "source_tenant_id")
    op.create_check_constraint(
        "ck_pending_intent_resolved_complete",
        "pending_intents",
        "state != 'RESOLVED' or "
        "(selected_candidate_key is not null and "
        "resolution_inbound_event_id is not null and "
        "resolution_kind is not null and resolved_at is not null)",
    )
