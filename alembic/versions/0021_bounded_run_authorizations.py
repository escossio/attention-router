"""Add persisted bounded external-run authorization."""
import sqlalchemy as sa
from alembic import op

revision = "0021_bounded_run_authorizations"
down_revision = "0020_standing_directives"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bounded_run_authorizations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("scenario_run_id", sa.String(64), sa.ForeignKey("scenario_runs.id"), nullable=False),
        sa.Column("effect_budget_id", sa.String(64), sa.ForeignKey("effect_budgets.id"), nullable=False),
        sa.Column("level", sa.String(40), nullable=False),
        sa.Column("actor_scope", sa.String(180), nullable=False),
        sa.Column("target_scope", sa.String(240), nullable=False),
        sa.Column("capability_scope", sa.String(160), nullable=False),
        sa.Column("effect_scope", sa.String(160), nullable=False),
        sa.Column("max_effects", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(24), nullable=False, server_default="ACTIVE"),
        sa.Column("authorized_by", sa.String(120), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status in ('ACTIVE','REVOKED','EXPIRED','CONSUMED')", name="ck_bounded_run_authorization_status"),
        sa.CheckConstraint("max_effects > 0", name="ck_bounded_run_authorization_max_effects"),
        sa.CheckConstraint("expires_at > valid_from", name="ck_bounded_run_authorization_validity"),
        sa.CheckConstraint("authorized_by not in ('SYNTHETIC_ACTOR','ANDY','PROVIDER','TRANSPORT')", name="ck_bounded_run_authorization_authority"),
        sa.UniqueConstraint("tenant_id", "scenario_run_id", "effect_budget_id", name="uq_bounded_run_authorization_run_budget"),
    )
    op.create_index("ix_bounded_run_authorization_lookup", "bounded_run_authorizations", ["tenant_id", "scenario_run_id", "status"])


def downgrade():
    op.drop_index("ix_bounded_run_authorization_lookup", table_name="bounded_run_authorizations")
    op.drop_table("bounded_run_authorizations")
