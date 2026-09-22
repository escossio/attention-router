"""Artifact Understanding V1 derived multimodal analysis.

Revision ID: 0047_artifact_understanding_v1
Revises: 0046_gmail_history_cursor
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0047_artifact_understanding_v1"
down_revision = "0046_gmail_history_cursor"
branch_labels = None
depends_on = None


def upgrade():
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "artifact_understandings",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("artifact_id", sa.String(64)),
        sa.Column(
            "inbound_event_id",
            sa.String(64),
            sa.ForeignKey("inbound_events.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("content_kind", sa.String(24), nullable=False),
        sa.Column("summary_text", sa.Text()),
        sa.Column("extracted_text", sa.Text()),
        sa.Column("visual_description", sa.Text()),
        sa.Column("key_facts", json_type, nullable=False),
        sa.Column("language", sa.String(40)),
        sa.Column("text_truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("provider", sa.String(32)),
        sa.Column("model", sa.String(80)),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("provider_request_reference", sa.String(180)),
        sa.Column("error_code", sa.String(120)),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_by", sa.String(120)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["artifacts.tenant_id", "artifacts.id"],
            name="fk_artifact_understanding_tenant_artifact",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "inbound_event_id",
            name="uq_artifact_understanding_tenant_event",
        ),
        sa.CheckConstraint(
            "status in ('PENDING','PROCESSING','READY','FAILED')",
            name="ck_artifact_understanding_status",
        ),
        sa.CheckConstraint(
            "content_kind in ('IMAGE','DOCUMENT')",
            name="ck_artifact_understanding_kind",
        ),
        sa.CheckConstraint(
            "(status='READY' and summary_text is not null) or "
            "(status<>'READY' and summary_text is null)",
            name="ck_artifact_understanding_ready_summary",
        ),
    )
    op.create_index(
        "ix_artifact_understanding_tenant_status_created",
        "artifact_understandings",
        ["tenant_id", "status", "created_at"],
    )
    op.create_index(
        "ix_artifact_understanding_tenant_artifact",
        "artifact_understandings",
        ["tenant_id", "artifact_id"],
    )


def downgrade():
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT 1 FROM artifact_understandings LIMIT 1")
    ).first():
        raise RuntimeError(
            "ARTIFACT_UNDERSTANDING_DOWNGRADE_REQUIRES_DATA_EXPORT"
        )
    op.drop_index(
        "ix_artifact_understanding_tenant_artifact",
        table_name="artifact_understandings",
    )
    op.drop_index(
        "ix_artifact_understanding_tenant_status_created",
        table_name="artifact_understandings",
    )
    op.drop_table("artifact_understandings")
