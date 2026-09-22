"""Provider-neutral human approval decision evidence for Native App Approval V1A.

Revision ID: 0048_native_app_approval_v1a
Revises: 0047_artifact_understanding_v1
"""

from alembic import op
import sqlalchemy as sa


revision = "0048_native_app_approval_v1a"
down_revision = "0047_artifact_understanding_v1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "human_approval_decision_evidence",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "authorization_id",
            sa.String(64),
            sa.ForeignKey("human_execution_authorizations.id"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("approver_reference", sa.String(120), nullable=False),
        sa.Column("human_identity_id", sa.String(64)),
        sa.Column("device_id", sa.String(64)),
        sa.Column("client_session_id", sa.String(64)),
        sa.Column("provider_event_reference", sa.String(180)),
        sa.Column("idempotency_key", sa.String(180), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "authorization_id",
            name="uq_human_approval_decision_authorization",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_human_approval_decision_idempotency",
        ),
        sa.CheckConstraint(
            "decision in ('APPROVE','DENY')",
            name="ck_human_approval_decision_value",
        ),
        sa.CheckConstraint(
            "channel in ('META_WHATSAPP','ANDROID_CLIENT')",
            name="ck_human_approval_decision_channel",
        ),
        sa.CheckConstraint(
            "(channel='ANDROID_CLIENT' and human_identity_id is not null "
            "and device_id is not null and client_session_id is not null "
            "and provider_event_reference is null) or "
            "(channel='META_WHATSAPP' and provider_event_reference is not null "
            "and human_identity_id is null and device_id is null "
            "and client_session_id is null)",
            name="ck_human_approval_decision_channel_evidence",
        ),
    )
    op.create_index(
        "ix_human_approval_decision_tenant_decided",
        "human_approval_decision_evidence",
        ["tenant_id", "decided_at"],
    )


def downgrade():
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT 1 FROM human_approval_decision_evidence LIMIT 1")
    ).first():
        raise RuntimeError(
            "NATIVE_APP_APPROVAL_DOWNGRADE_REQUIRES_DATA_EXPORT"
        )
    op.drop_index(
        "ix_human_approval_decision_tenant_decided",
        table_name="human_approval_decision_evidence",
    )
    op.drop_table("human_approval_decision_evidence")
