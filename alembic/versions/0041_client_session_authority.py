"""Add V0.3C short client-session authority."""

from alembic import op
import sqlalchemy as sa

revision = "0041_client_session_authority"
down_revision = "0040_client_device_bootstrap"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "client_session_challenges",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column("human_identity_id", sa.String(64), nullable=False),
        sa.Column("requested_tenant_id", sa.String(64), nullable=True),
        sa.Column("challenge_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_client_session_challenges"),
        sa.ForeignKeyConstraint(["device_id"], ["client_devices.id"], name="fk_client_session_challenge_device"),
        sa.ForeignKeyConstraint(["human_identity_id"], ["human_identities.id"], name="fk_client_session_challenge_human_identity"),
        sa.UniqueConstraint("challenge_digest", name="uq_client_session_challenge_digest"),
        sa.CheckConstraint("state IN ('PENDING', 'VERIFIED', 'REJECTED')", name="ck_client_session_challenge_state"),
        sa.CheckConstraint("expires_at > created_at", name="ck_client_session_challenge_lifetime"),
    )
    op.create_index("ix_client_session_challenge_device_state", "client_session_challenges", ["device_id", "state"])
    op.create_index("ix_client_session_challenge_human_state", "client_session_challenges", ["human_identity_id", "state"])

    op.create_table(
        "client_sessions",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("source_challenge_id", sa.String(64), nullable=False),
        sa.Column("human_identity_id", sa.String(64), nullable=False),
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_client_sessions"),
        sa.ForeignKeyConstraint(["source_challenge_id"], ["client_session_challenges.id"], name="fk_client_session_source_challenge"),
        sa.ForeignKeyConstraint(["human_identity_id"], ["human_identities.id"], name="fk_client_session_human_identity"),
        sa.ForeignKeyConstraint(["device_id"], ["client_devices.id"], name="fk_client_session_device"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_client_session_tenant"),
        sa.UniqueConstraint("token_digest", name="uq_client_session_token_digest"),
        sa.UniqueConstraint("source_challenge_id", name="uq_client_session_source_challenge"),
        sa.CheckConstraint("state IN ('ACTIVE', 'REVOKED')", name="ck_client_session_state"),
        sa.CheckConstraint("expires_at > created_at", name="ck_client_session_lifetime"),
        sa.CheckConstraint(
            "(state = 'ACTIVE' AND revoked_at IS NULL) OR (state = 'REVOKED' AND revoked_at IS NOT NULL)",
            name="ck_client_session_revocation_state",
        ),
    )
    op.create_index("ix_client_session_human_tenant_state", "client_sessions", ["human_identity_id", "tenant_id", "state"])
    op.create_index("ix_client_session_device_state", "client_sessions", ["device_id", "state"])


def downgrade():
    op.drop_index("ix_client_session_device_state", table_name="client_sessions")
    op.drop_index("ix_client_session_human_tenant_state", table_name="client_sessions")
    op.drop_table("client_sessions")
    op.drop_index("ix_client_session_challenge_human_state", table_name="client_session_challenges")
    op.drop_index("ix_client_session_challenge_device_state", table_name="client_session_challenges")
    op.drop_table("client_session_challenges")
