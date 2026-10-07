"""Owner-bound temporary selection for Personal Context bootstrap.

Revision ID: 0057_bootstrap_selection
Revises: 0056_attention_salience_v0
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0057_bootstrap_selection"
down_revision = "0056_attention_salience_v0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "personal_context_bootstrap_selections",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("owner_human_identity_id", sa.String(64), sa.ForeignKey("human_identities.id"), nullable=False),
        sa.Column("chat_keys", json_type, nullable=False),
        sa.Column("display_chats", json_type, nullable=False),
        sa.Column("expected_consent_ref", sa.String(240), nullable=False),
        sa.Column("processing_budget", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("bootstrap_run_id", sa.String(64), sa.ForeignKey("personal_context_bootstrap_runs.id")),
        sa.Column("confirmation_fingerprint", sa.String(64)),
        sa.CheckConstraint("expires_at > created_at", name="ck_pc_bootstrap_selection_expiry"),
        sa.CheckConstraint(
            "(consumed_at IS NULL AND bootstrap_run_id IS NULL AND confirmation_fingerprint IS NULL) "
            "OR (consumed_at IS NOT NULL AND bootstrap_run_id IS NOT NULL AND confirmation_fingerprint IS NOT NULL)",
            name="ck_pc_bootstrap_selection_consumption",
        ),
    )
    op.create_index(
        "ix_pc_bootstrap_selection_owner",
        "personal_context_bootstrap_selections",
        ["tenant_id", "owner_human_identity_id", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_pc_bootstrap_selection_owner", table_name="personal_context_bootstrap_selections")
    op.drop_table("personal_context_bootstrap_selections")
