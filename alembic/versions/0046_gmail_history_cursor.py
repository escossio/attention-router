"""Installation-owned Gmail history cursor; existing mailboxes start unseeded."""

from alembic import op
import sqlalchemy as sa

revision = "0046_gmail_history_cursor"
down_revision = "0045_provider_authorization_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("provider_authorizations", sa.Column("gmail_history_id", sa.String(20)))


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM provider_authorizations WHERE gmail_history_id IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError("GMAIL_HISTORY_DOWNGRADE_REQUIRES_DATA_EXPORT")
    op.drop_column("provider_authorizations", "gmail_history_id")
