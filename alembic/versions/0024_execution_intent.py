"""Add inert canonical execution intents (not applied live)."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0024_execution_intent"
down_revision = "0023_human_execution_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "execution_intents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("idempotency_key", sa.String(180), nullable=False),
        sa.Column("scope", sa.JSON().with_variant(postgresql.JSONB, "postgresql"), nullable=False),
        sa.Column("scope_fingerprint", sa.String(128), nullable=False),
        sa.Column("provenance", sa.JSON().with_variant(postgresql.JSONB, "postgresql"), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="PREPARED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True)),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("idempotency_key", name="uq_execution_intents_idempotency"),
        sa.UniqueConstraint("scope_fingerprint", name="uq_execution_intents_fingerprint"),
        sa.CheckConstraint("state IN ('PREPARED','FROZEN','RETIRED','MATERIALIZED')", name="ck_execution_intent_state"),
    )


def downgrade() -> None:
    op.drop_table("execution_intents")
