"""Add canonical human execution authorization."""
import sqlalchemy as sa
from alembic import op
revision = "0023_human_execution_auth"
down_revision = "0022_transport_canary_auth"
branch_labels = None
depends_on = None
def upgrade():
    op.create_table("human_execution_authorizations", sa.Column("id",sa.String(64),primary_key=True), sa.Column("scope",sa.JSON(),nullable=False), sa.Column("scope_fingerprint",sa.String(128),nullable=False), sa.Column("expected_approver",sa.String(120),nullable=False), sa.Column("approval_channel",sa.String(80),nullable=False), sa.Column("request_wamid",sa.String(180)), sa.Column("state",sa.String(32),nullable=False,server_default="PREPARED"), sa.Column("issued_at",sa.DateTime(timezone=True),nullable=False), sa.Column("expires_at",sa.DateTime(timezone=True),nullable=False), sa.Column("decision_at",sa.DateTime(timezone=True)), sa.Column("decision_inbound_wamid",sa.String(180)), sa.Column("decision_sender",sa.String(120)), sa.Column("decision_button_id",sa.String(180)), sa.Column("correlation_id",sa.String(64),nullable=False), sa.Column("created_at",sa.DateTime(timezone=True),nullable=False), sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False), sa.CheckConstraint("state in ('PREPARED','PENDING_HUMAN_APPROVAL','APPROVED','DENIED','EXPIRED','CONSUMED','REVOKED')",name="ck_human_execution_authorization_state"), sa.CheckConstraint("expires_at > issued_at",name="ck_human_execution_authorization_validity"), sa.UniqueConstraint("scope_fingerprint",name="uq_human_execution_authorization_fingerprint"))
def downgrade(): op.drop_table("human_execution_authorizations")
