"""Owner-configurable public reference name.

Revision ID: 0051_owner_profile
Revises: 0050_client_pending_source
"""

from alembic import op
import sqlalchemy as sa


revision = "0051_owner_profile"
down_revision = "0050_client_pending_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "human_profiles",
        sa.Column(
            "human_identity_id",
            sa.String(64),
            sa.ForeignKey(
                "human_identities.id",
                name="fk_human_profile_human_identity",
            ),
            primary_key=True,
        ),
        sa.Column("assistant_reference_name", sa.String(160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "assistant_reference_name is null or "
            "length(trim(assistant_reference_name)) > 0",
            name="ck_human_profile_reference_name_nonempty",
        ),
    )


def downgrade() -> None:
    op.drop_table("human_profiles")
