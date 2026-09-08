"""persist self-contained decision contract fields

Revision ID: 0011_decision_contract
Revises: 0010_memory_archive
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0011_decision_contract"
down_revision: str | None = "0010_memory_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_decisions", sa.Column("intent", sa.String(80), nullable=True))
    op.add_column("agent_decisions", sa.Column("objective", sa.String(120), nullable=True))
    op.add_column("agent_decisions", sa.Column("self_contained", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("agent_decisions", sa.Column("context_sufficient", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("agent_decisions", sa.Column("context_requirements", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("agent_decisions", "context_requirements")
    op.drop_column("agent_decisions", "context_sufficient")
    op.drop_column("agent_decisions", "self_contained")
    op.drop_column("agent_decisions", "objective")
    op.drop_column("agent_decisions", "intent")
