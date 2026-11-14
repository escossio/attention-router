"""Create separate owner-reviewed private knowledge admission, no execution grant.

Revision ID: 0062_artifact_sensitive_review
Revises: 0061_tts_outbox_constraint
"""

from alembic import op
import sqlalchemy as sa

revision = "0062_artifact_sensitive_review"
down_revision = "0061_tts_outbox_constraint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifact_sensitive_reviews",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("artifact_id", sa.String(64), nullable=False),
        sa.Column("understanding_id", sa.String(64), nullable=False),
        sa.Column("source_inbound_event_id", sa.String(64), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("expected_owner_human_identity_id", sa.String(64)),
        sa.Column("decision_human_identity_id", sa.String(64)),
        sa.Column("decision_device_id", sa.String(64)),
        sa.Column("decision_client_session_id", sa.String(64)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "artifact_id"], ["artifacts.tenant_id", "artifacts.id"],
            name="fk_artifact_sensitive_review_artifact_scope",
        ),
        sa.ForeignKeyConstraint(
            ["understanding_id"], ["artifact_understandings.id"],
            name="fk_artifact_sensitive_review_understanding",
        ),
        sa.ForeignKeyConstraint(
            ["source_inbound_event_id"], ["inbound_events.id"],
            name="fk_artifact_sensitive_review_receipt",
        ),
        sa.UniqueConstraint("tenant_id", "understanding_id", name="uq_artifact_sensitive_review_source"),
        sa.CheckConstraint(
            "category IN ('PAYMENT_CARD','IDENTITY_DOCUMENT','ACCESS_SECRET','CONTACT_CARD')",
            name="ck_artifact_sensitive_review_category",
        ),
        sa.CheckConstraint(
            "state IN ('PENDING','UNASSIGNED','APPROVED_PRIVATE','ARCHIVE_ONLY','EXPIRED')",
            name="ck_artifact_sensitive_review_state",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at", name="ck_artifact_sensitive_review_time",
        ),
    )
    op.create_index(
        "ix_artifact_sensitive_review_owner_pending", "artifact_sensitive_reviews",
        ["tenant_id", "expected_owner_human_identity_id", "state"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text(
        "SELECT 1 FROM artifact_sensitive_reviews LIMIT 1"
    )).first():
        raise RuntimeError("ARTIFACT_SENSITIVE_REVIEW_DOWNGRADE_REQUIRES_EXPLICIT_ARCHIVE")
    op.drop_index(
        "ix_artifact_sensitive_review_owner_pending", table_name="artifact_sensitive_reviews"
    )
    op.drop_table("artifact_sensitive_reviews")
