"""Bind existing human authorization to a frozen execution intent."""

from alembic import op
import sqlalchemy as sa

revision = "0025_human_auth_execution_intent"
down_revision = "0024_execution_intent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT count(*) FROM human_execution_authorizations")).scalar_one() != 0:
        raise RuntimeError("0025 requires zero existing human_execution_authorizations")
    op.alter_column("human_execution_authorizations", "scope_fingerprint", nullable=True)
    op.add_column("human_execution_authorizations", sa.Column("execution_intent_id", sa.String(64), nullable=False))
    op.add_column("human_execution_authorizations", sa.Column("execution_intent_fingerprint", sa.String(128), nullable=False))
    op.create_foreign_key("fk_human_auth_execution_intent", "human_execution_authorizations", "execution_intents", ["execution_intent_id"], ["id"])
    op.create_index("ix_human_auth_execution_intent", "human_execution_authorizations", ["execution_intent_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT count(*) FROM human_execution_authorizations")).scalar_one() != 0:
        raise RuntimeError("0025 downgrade requires zero human_execution_authorizations")
    op.drop_index("ix_human_auth_execution_intent", table_name="human_execution_authorizations")
    op.drop_constraint("fk_human_auth_execution_intent", "human_execution_authorizations", type_="foreignkey")
    op.drop_column("human_execution_authorizations", "execution_intent_fingerprint")
    op.drop_column("human_execution_authorizations", "execution_intent_id")
    op.alter_column("human_execution_authorizations", "scope_fingerprint", nullable=False)
