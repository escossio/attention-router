"""actor bindings for real inbound identity

Revision ID: 0003_actor_bindings
Revises: 0002_core_hardening
Create Date: 2026-08-07 02:42:54
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0003_actor_bindings"
down_revision: str | None = "0002_core_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")
    op.create_table(
        "actor_bindings",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column("external_actor_id", sa.String(length=180), nullable=False),
        sa.Column("actor_key", sa.String(length=120), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=True),
        sa.Column("actor_category", sa.String(length=120), nullable=False),
        sa.Column("active_context", sa.String(length=120), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("metadata", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_actor_id", name="uq_actor_bindings_source_external"),
    )


def downgrade() -> None:
    op.drop_table("actor_bindings")
