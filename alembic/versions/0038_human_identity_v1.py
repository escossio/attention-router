"""Add provider-neutral Human Identity persistence."""

from alembic import op
import sqlalchemy as sa

revision = "0038_human_identity_v1"
down_revision = "0037_integration_admission_v0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "human_identities",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_human_identities"),
    )
    op.create_table(
        "external_identity_bindings",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("human_identity_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_external_identity_bindings"),
        sa.ForeignKeyConstraint(
            ["human_identity_id"], ["human_identities.id"],
            name="fk_external_identity_binding_human_identity",
        ),
        sa.UniqueConstraint(
            "provider", "subject", name="uq_external_identity_provider_subject",
        ),
    )
    op.create_table(
        "human_auth_transactions",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("nonce_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_human_identity_id", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_human_auth_transactions"),
        sa.ForeignKeyConstraint(
            ["resolved_human_identity_id"], ["human_identities.id"],
            name="fk_human_auth_transaction_human_identity",
        ),
        sa.UniqueConstraint("nonce_digest", name="uq_human_auth_transaction_nonce_digest"),
        sa.CheckConstraint(
            "state IN ('PENDING', 'VERIFIED', 'REJECTED')",
            name="ck_human_auth_transaction_state",
        ),
    )


def downgrade():
    op.drop_table("human_auth_transactions")
    op.drop_table("external_identity_bindings")
    op.drop_table("human_identities")
