"""Add tenant-scoped Standing Directive V1."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020_standing_directives"
down_revision = "0019_platform_evolution_wave_d"
branch_labels = None
depends_on = None


def _json():
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade():
    op.create_table(
        "standing_directives",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("subject_actor_id", sa.String(120), nullable=False),
        sa.Column("created_by_actor_id", sa.String(120), nullable=False),
        sa.Column("trigger_type", sa.String(64), nullable=False),
        sa.Column("effect_type", sa.String(96), nullable=False),
        sa.Column("audience_selector", _json(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="ACTIVE"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("provenance", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status in ('ACTIVE','REVOKED')", name="ck_standing_directive_status"),
        sa.CheckConstraint("version > 0", name="ck_standing_directive_version"),
    )
    op.create_index("ix_standing_directive_resolution", "standing_directives",
                    ["tenant_id", "subject_actor_id", "trigger_type", "status"])


def downgrade():
    op.drop_index("ix_standing_directive_resolution", table_name="standing_directives")
    op.drop_table("standing_directives")
