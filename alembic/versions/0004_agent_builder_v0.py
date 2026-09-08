"""agent builder v0

Revision ID: 0004_agent_builder_v0
Revises: 0003_actor_bindings
Create Date: 2026-08-09 00:00:00
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0004_agent_builder_v0"
down_revision: str | None = "0003_actor_bindings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")
    op.create_table(
        "agent_blueprints",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("current_version_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "agent_blueprint_versions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("blueprint_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec", json_type, nullable=False),
        sa.Column("checksum", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("change_reason", sa.String(length=240), nullable=False),
        sa.Column("is_immutable", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["blueprint_id"], ["agent_blueprints.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("blueprint_id", "version", name="uq_agent_blueprint_versions_blueprint_version"),
    )
    op.create_table(
        "configuration_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("blueprint_id", sa.String(length=64), nullable=True),
        sa.Column("current_stage", sa.String(length=80), nullable=False),
        sa.Column("answers", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["blueprint_id"], ["agent_blueprints.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("configuration_sessions")
    op.drop_table("agent_blueprint_versions")
    op.drop_table("agent_blueprints")
