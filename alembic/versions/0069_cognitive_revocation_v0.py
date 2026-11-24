"""Independent technical validity, preserving owner decisions and history."""
from alembic import op
import sqlalchemy as sa

revision = "0069_cognitive_revocation_v0"
down_revision = "0068_google_contacts_cache"
branch_labels = None
depends_on = None


def upgrade():
    for table, name in (("semantic_episodes", "episode"), ("candidate_insights", "candidate")):
        op.add_column(table, sa.Column("evidence_status", sa.String(24), nullable=False, server_default="CURRENT"))
        op.create_check_constraint(f"ck_{name}_evidence_status", table,
                                   "evidence_status IN ('CURRENT','EVIDENCE_REVOKED')")


def downgrade():
    op.execute("LOCK TABLE candidate_insights, semantic_episodes IN ACCESS EXCLUSIVE MODE")
    for table in ("semantic_episodes", "candidate_insights"):
        if op.get_bind().execute(sa.text(
            f"SELECT 1 FROM {table} WHERE evidence_status <> 'CURRENT' LIMIT 1"
        )).first():
            raise RuntimeError("COGNITIVE_REVOCATION_DOWNGRADE_REQUIRES_NO_REVOKED_ROWS")
    for table, name in (("semantic_episodes", "episode"), ("candidate_insights", "candidate")):
        op.drop_constraint(f"ck_{name}_evidence_status", table, type_="check")
        op.drop_column(table, "evidence_status")
