"""Add persistent single-use transport canary authorization."""
import sqlalchemy as sa
from alembic import op

revision = "0022_transport_canary_auth"
down_revision = "0021_bounded_run_authorizations"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("transport_canary_authorizations",
        sa.Column("id", sa.String(64), primary_key=True), sa.Column("transport", sa.String(80), nullable=False),
        sa.Column("operation", sa.String(80), nullable=False), sa.Column("phone_number_id", sa.String(80), nullable=False),
        sa.Column("text", sa.Text(), nullable=False), sa.Column("recipient", sa.String(160), nullable=False),
        sa.Column("scope_fingerprint", sa.String(128), nullable=False), sa.Column("status", sa.String(24), nullable=False, server_default="PREPARED"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authorized_by", sa.String(120)), sa.Column("authorized_at", sa.DateTime(timezone=True)), sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("correlation_id", sa.String(64), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status in ('PREPARED','AUTHORIZED','REVOKED','EXPIRED','CONSUMED')", name="ck_transport_canary_authorization_status"),
        sa.CheckConstraint("expires_at > valid_from", name="ck_transport_canary_authorization_validity"), sa.UniqueConstraint("scope_fingerprint", name="uq_transport_canary_authorization_fingerprint"))

def downgrade():
    op.drop_table("transport_canary_authorizations")
