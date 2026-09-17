"""Persist digest-only short-lived Human Auth continuation grants."""

from alembic import op
import sqlalchemy as sa


revision = "0039_human_auth_continuation_grant"
down_revision = "0038_human_identity_v1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "human_auth_continuation_grants",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("human_identity_id", sa.String(64), nullable=False),
        sa.Column("source_auth_transaction_id", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_human_auth_continuation_grants"),
        sa.ForeignKeyConstraint(
            ["human_identity_id"], ["human_identities.id"],
            name="fk_hacg_human_identity",
        ),
        sa.ForeignKeyConstraint(
            ["source_auth_transaction_id"], ["human_auth_transactions.id"],
            name="fk_hacg_auth_transaction",
        ),
        sa.UniqueConstraint("token_digest", name="uq_hacg_token_digest"),
        sa.UniqueConstraint(
            "source_auth_transaction_id", name="uq_hacg_source_auth_transaction",
        ),
        sa.CheckConstraint("purpose = 'DEVICE_BOOTSTRAP'", name="ck_hacg_purpose"),
        sa.CheckConstraint("state IN ('ACTIVE', 'CONSUMED', 'REVOKED')", name="ck_hacg_state"),
        sa.CheckConstraint("expires_at > created_at", name="ck_hacg_lifetime"),
    )


def downgrade():
    op.drop_table("human_auth_continuation_grants")
