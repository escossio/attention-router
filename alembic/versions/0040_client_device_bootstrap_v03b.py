"""Add V0.3B pre-session device bootstrap authority."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0040_client_device_bootstrap"
down_revision = "0039_human_auth_cont_grant"
branch_labels = None
depends_on = None


def _json():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    op.create_table(
        "client_tenant_memberships",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("human_identity_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_client_tenant_memberships"),
        sa.ForeignKeyConstraint(
            ["human_identity_id"],
            ["human_identities.id"],
            name="fk_client_membership_human_identity",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_client_membership_tenant",
        ),
        sa.UniqueConstraint(
            "human_identity_id",
            "tenant_id",
            name="uq_client_membership_human_tenant",
        ),
        sa.CheckConstraint(
            "role IN ('OWNER', 'ADMIN', 'MEMBER')",
            name="ck_client_membership_role",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'REVOKED')",
            name="ck_client_membership_status",
        ),
    )
    op.create_index(
        "ix_client_membership_human_status",
        "client_tenant_memberships",
        ["human_identity_id", "status"],
    )

    op.create_table(
        "client_devices",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("human_identity_id", sa.String(64), nullable=False),
        sa.Column("public_key_fingerprint", sa.String(71), nullable=False),
        sa.Column("public_key_spki", sa.LargeBinary(), nullable=False),
        sa.Column("canonical_name", sa.String(160), nullable=False),
        sa.Column("platform", sa.String(24), nullable=False),
        sa.Column("roles", _json(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_client_devices"),
        sa.ForeignKeyConstraint(
            ["human_identity_id"],
            ["human_identities.id"],
            name="fk_client_device_human_identity",
        ),
        sa.UniqueConstraint(
            "public_key_fingerprint",
            name="uq_client_device_public_key_fingerprint",
        ),
        sa.CheckConstraint("platform = 'ANDROID'", name="ck_client_device_platform"),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')",
            name="ck_client_device_status",
        ),
    )
    op.create_index(
        "ix_client_device_human_status",
        "client_devices",
        ["human_identity_id", "status"],
    )

    op.create_table(
        "device_bootstrap_challenges",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("continuation_grant_id", sa.String(64), nullable=False),
        sa.Column("human_identity_id", sa.String(64), nullable=False),
        sa.Column("public_key_fingerprint", sa.String(71), nullable=False),
        sa.Column("public_key_spki", sa.LargeBinary(), nullable=False),
        sa.Column("canonical_device_name", sa.String(160), nullable=False),
        sa.Column("platform", sa.String(24), nullable=False),
        sa.Column("roles", _json(), nullable=False),
        sa.Column("challenge_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_device_bootstrap_challenges"),
        sa.ForeignKeyConstraint(
            ["continuation_grant_id"],
            ["human_auth_continuation_grants.id"],
            name="fk_device_bootstrap_continuation_grant",
        ),
        sa.ForeignKeyConstraint(
            ["human_identity_id"],
            ["human_identities.id"],
            name="fk_device_bootstrap_human_identity",
        ),
        sa.UniqueConstraint(
            "continuation_grant_id",
            name="uq_device_bootstrap_continuation_grant",
        ),
        sa.UniqueConstraint(
            "challenge_digest",
            name="uq_device_bootstrap_challenge_digest",
        ),
        sa.CheckConstraint("platform = 'ANDROID'", name="ck_device_bootstrap_platform"),
        sa.CheckConstraint(
            "state IN ('PENDING', 'VERIFIED', 'REJECTED')",
            name="ck_device_bootstrap_state",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_device_bootstrap_lifetime",
        ),
    )
    op.create_index(
        "ix_device_bootstrap_human_state",
        "device_bootstrap_challenges",
        ["human_identity_id", "state"],
    )


def downgrade():
    op.drop_index(
        "ix_device_bootstrap_human_state",
        table_name="device_bootstrap_challenges",
    )
    op.drop_table("device_bootstrap_challenges")
    op.drop_index("ix_client_device_human_status", table_name="client_devices")
    op.drop_table("client_devices")
    op.drop_index(
        "ix_client_membership_human_status",
        table_name="client_tenant_memberships",
    )
    op.drop_table("client_tenant_memberships")
