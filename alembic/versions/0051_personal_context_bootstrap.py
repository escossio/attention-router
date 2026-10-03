"""Personal Context V2B semantic bootstrap lifecycle.

Revision ID: 0051_personal_context_bootstrap
Revises: 0050_client_pending_source
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0051_personal_context_bootstrap"
down_revision = "0050_client_pending_source"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    json_type = _json_type()
    op.create_table(
        "personal_context_bootstrap_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.id", name="fk_pc_bootstrap_run_tenant"),
            nullable=False,
        ),
        sa.Column(
            "owner_human_identity_id",
            sa.String(64),
            sa.ForeignKey(
                "human_identities.id",
                name="fk_pc_bootstrap_run_human_identity",
            ),
            nullable=False,
        ),
        sa.Column("represented_owner_actor_key", sa.String(120), nullable=False),
        sa.Column("source_kind", sa.String(80), nullable=False),
        sa.Column("source_account", sa.String(180), nullable=False),
        sa.Column("source_revision", sa.String(160), nullable=True),
        sa.Column("source_selection", json_type, nullable=False),
        sa.Column("consent_ref", sa.String(240), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column(
            "requested_control",
            sa.String(16),
            nullable=False,
            server_default="NONE",
        ),
        sa.Column("resume_cursor", json_type, nullable=True),
        sa.Column("processing_budget", json_type, nullable=False),
        sa.Column("progress", json_type, nullable=False),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_personal_context_bootstrap_run_idempotency",
        ),
        sa.CheckConstraint(
            "state IN ('CREATED','QUEUED','RUNNING','PAUSED','COMPLETED','CANCELLED','FAILED')",
            name="ck_personal_context_bootstrap_run_state",
        ),
        sa.CheckConstraint(
            "requested_control IN ('NONE','PAUSE','CANCEL')",
            name="ck_personal_context_bootstrap_run_control",
        ),
        sa.CheckConstraint(
            "length(trim(consent_ref)) > 0",
            name="ck_personal_context_bootstrap_consent_nonempty",
        ),
    )
    op.create_index(
        "ix_personal_context_bootstrap_owner_state",
        "personal_context_bootstrap_runs",
        ["tenant_id", "represented_owner_actor_key", "state"],
    )
    op.create_index(
        "ix_personal_context_bootstrap_human_state",
        "personal_context_bootstrap_runs",
        ["owner_human_identity_id", "state"],
    )

    op.create_table(
        "personal_context_bootstrap_batches",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "bootstrap_run_id",
            sa.String(64),
            sa.ForeignKey(
                "personal_context_bootstrap_runs.id",
                name="fk_pc_bootstrap_batch_run",
            ),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("cursor_before", json_type, nullable=True),
        sa.Column("cursor_after", json_type, nullable=True),
        sa.Column("metrics", json_type, nullable=False),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "bootstrap_run_id",
            "ordinal",
            name="uq_personal_context_bootstrap_batch_ordinal",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_personal_context_bootstrap_batch_idempotency",
        ),
        sa.CheckConstraint(
            "ordinal > 0",
            name="ck_personal_context_bootstrap_batch_ordinal_positive",
        ),
        sa.CheckConstraint(
            "state IN ('CREATED','RUNNING','COMPLETED','FAILED')",
            name="ck_personal_context_bootstrap_batch_state",
        ),
    )
    op.create_index(
        "ix_personal_context_bootstrap_batch_run_state",
        "personal_context_bootstrap_batches",
        ["bootstrap_run_id", "state", "ordinal"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_personal_context_bootstrap_batch_run_state",
        table_name="personal_context_bootstrap_batches",
    )
    op.drop_table("personal_context_bootstrap_batches")
    op.drop_index(
        "ix_personal_context_bootstrap_human_state",
        table_name="personal_context_bootstrap_runs",
    )
    op.drop_index(
        "ix_personal_context_bootstrap_owner_state",
        table_name="personal_context_bootstrap_runs",
    )
    op.drop_table("personal_context_bootstrap_runs")
