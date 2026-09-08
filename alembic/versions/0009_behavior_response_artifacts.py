"""persist behavior realization metadata for TTS-ready responses

Revision ID: 0009_behavior_response_artifacts
Revises: 0008_controlled_autonomy
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0009_behavior_response_artifacts"
down_revision: str | None = "0008_controlled_autonomy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_decisions", sa.Column("response_message_family", sa.String(80), nullable=True))
    op.add_column("agent_decisions", sa.Column("response_variant_id", sa.String(120), nullable=True))
    op.add_column("agent_decisions", sa.Column("response_spoken_text", sa.Text(), nullable=True))
    op.add_column("agent_decisions", sa.Column("response_introduction_included", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_decisions", "response_introduction_included")
    op.drop_column("agent_decisions", "response_spoken_text")
    op.drop_column("agent_decisions", "response_variant_id")
    op.drop_column("agent_decisions", "response_message_family")
