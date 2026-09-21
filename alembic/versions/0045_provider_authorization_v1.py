"""Add product provider authorization installations."""

from alembic import op
import sqlalchemy as sa


revision = "0045_provider_authorization_v1"
down_revision = "0044_integration_dispatch_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_authorizations",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("slot_key", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("human_identity_id", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("product", sa.String(24), nullable=False),
        sa.Column("provider_account_hash", sa.String(64), nullable=False),
        sa.Column("granted_scopes", sa.JSON(), nullable=False),
        sa.Column("secret_nonce_b64url", sa.String(64), nullable=False),
        sa.Column("secret_ciphertext_b64url", sa.String(8192), nullable=False),
        sa.Column("secret_key_version", sa.String(16), nullable=False),
        sa.Column("integration_binding_id", sa.String(64), nullable=False),
        sa.Column("integration_credential_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["human_identity_id"], ["human_identities.id"]),
        sa.ForeignKeyConstraint(
            ["integration_binding_id"], ["integration_bindings.id"]
        ),
        sa.ForeignKeyConstraint(
            ["integration_credential_id"], ["integration_credentials.id"]
        ),
        sa.PrimaryKeyConstraint("id", name="pk_provider_authorizations"),
        sa.UniqueConstraint("slot_key", name="uq_provider_authorization_slot"),
        sa.CheckConstraint(
            "provider = 'GOOGLE'",
            name="ck_provider_authorization_provider",
        ),
        sa.CheckConstraint(
            "product = 'GMAIL'",
            name="ck_provider_authorization_product",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')",
            name="ck_provider_authorization_status",
        ),
        sa.CheckConstraint(
            "(status = 'ACTIVE' AND revoked_at IS NULL) OR "
            "(status = 'REVOKED' AND revoked_at IS NOT NULL)",
            name="ck_provider_authorization_revocation",
        ),
    )
    op.create_index(
        "ix_provider_authorization_owner_product",
        "provider_authorizations",
        ["tenant_id", "human_identity_id", "provider", "product", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_provider_authorization_owner_product",
        table_name="provider_authorizations",
    )
    op.drop_table("provider_authorizations")
