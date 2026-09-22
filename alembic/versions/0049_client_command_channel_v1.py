"""Authenticated provider-neutral client command channel V1.

Revision ID: 0049_client_command_channel_v1
Revises: 0048_native_app_approval_v1a
"""

from alembic import op
import sqlalchemy as sa


revision = "0049_client_command_channel_v1"
down_revision = "0048_native_app_approval_v1a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "client_command_messages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("human_identity_id", sa.String(64), nullable=False),
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column("client_session_id", sa.String(64), nullable=False),
        sa.Column("client_request_id", sa.String(80), nullable=False),
        sa.Column("modality", sa.String(16), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("normalized_action", sa.String(80)),
        sa.Column("response_text", sa.Text()),
        sa.Column("error_code", sa.String(120)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "tenant_id",
            "human_identity_id",
            "client_request_id",
            name="uq_client_command_request",
        ),
        sa.CheckConstraint(
            "modality in ('TEXT','VOICE')",
            name="ck_client_command_modality",
        ),
        sa.CheckConstraint(
            "state in ('RECEIVED','COMPLETED','CLARIFICATION_REQUIRED',"
            "'GENERAL_TASK_PENDING','FAILED')",
            name="ck_client_command_state",
        ),
        sa.CheckConstraint(
            "length(input_text) between 1 and 4000",
            name="ck_client_command_input_length",
        ),
    )
    op.create_index(
        "ix_client_command_tenant_created",
        "client_command_messages",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_client_command_human_created",
        "client_command_messages",
        ["human_identity_id", "created_at"],
    )


def downgrade():
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT 1 FROM client_command_messages LIMIT 1")
    ).first():
        raise RuntimeError("CLIENT_COMMAND_DOWNGRADE_REQUIRES_DATA_EXPORT")
    op.drop_index(
        "ix_client_command_human_created",
        table_name="client_command_messages",
    )
    op.drop_index(
        "ix_client_command_tenant_created",
        table_name="client_command_messages",
    )
    op.drop_table("client_command_messages")
