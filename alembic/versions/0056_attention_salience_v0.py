"""Personal Context V2H attention and salience engine.

Revision ID: 0056_attention_salience_v0
Revises: 0055_obligation_expectation_v0
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0056_attention_salience_v0"
down_revision = "0055_obligation_expectation_v0"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    json_type = _json_type()

    op.create_table(
        "attention_assessments",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.id", name="fk_attention_assessment_tenant"),
            nullable=False,
        ),
        sa.Column("signal_key", sa.String(128), nullable=False),
        sa.Column("snapshot_fingerprint", sa.String(128), nullable=False),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("source_ref", sa.String(160), nullable=False),
        sa.Column("source_state", sa.String(64), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("score_class", sa.String(40), nullable=False),
        sa.Column("effective_class", sa.String(40), nullable=False),
        sa.Column("components", json_type, nullable=False),
        sa.Column("reason_codes", json_type, nullable=False),
        sa.Column("sensitivity_class", sa.String(20), nullable=False),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "owner_suppressed_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("owner_suppression_reason", sa.String(64), nullable=True),
        sa.Column("owner_suppressed_by", sa.String(120), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(120), nullable=True),
        sa.Column("owner_decision_ref", sa.String(240), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column(
            "supersedes_assessment_id",
            sa.String(64),
            sa.ForeignKey(
                "attention_assessments.id",
                name="fk_attention_assessment_supersedes",
            ),
            nullable=True,
        ),
        sa.Column("provenance", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "signal_key",
            "snapshot_fingerprint",
            name="uq_attention_assessment_snapshot",
        ),
        sa.CheckConstraint(
            "source_type IN ('OBLIGATION_INSTANCE','MEMORY_CLAIM_ANOMALY')",
            name="ck_attention_assessment_source_type",
        ),
        sa.CheckConstraint(
            "score >= 0 AND score <= 1",
            name="ck_attention_assessment_score",
        ),
        sa.CheckConstraint(
            "score_class IN ("
            "'IGNORE','REASONING_QUEUE','OWNER_SUGGESTION_CANDIDATE'"
            ")",
            name="ck_attention_assessment_score_class",
        ),
        sa.CheckConstraint(
            "effective_class IN ("
            "'IGNORE','REASONING_QUEUE','OWNER_SUGGESTION_CANDIDATE'"
            ")",
            name="ck_attention_assessment_effective_class",
        ),
        sa.CheckConstraint(
            "sensitivity_class IN ('NORMAL','PRIVATE')",
            name="ck_attention_assessment_sensitivity",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','ACKNOWLEDGED','SUPPRESSED','SUPERSEDED')",
            name="ck_attention_assessment_status",
        ),
    )
    op.create_index(
        "uq_attention_assessment_active_signal",
        "attention_assessments",
        ["tenant_id", "signal_key"],
        unique=True,
        postgresql_where=sa.text("status='ACTIVE'"),
        sqlite_where=sa.text("status='ACTIVE'"),
    )
    op.create_index(
        "ix_attention_assessment_tenant_class",
        "attention_assessments",
        ["tenant_id", "effective_class", "status", "updated_at"],
    )
    op.create_index(
        "ix_attention_assessment_source",
        "attention_assessments",
        ["tenant_id", "source_type", "source_ref"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT 1 FROM attention_assessments LIMIT 1")
    ).first():
        raise RuntimeError(
            "ATTENTION_SALIENCE_DOWNGRADE_REQUIRES_DATA_EXPORT"
        )

    op.drop_index(
        "ix_attention_assessment_source",
        table_name="attention_assessments",
    )
    op.drop_index(
        "ix_attention_assessment_tenant_class",
        table_name="attention_assessments",
    )
    op.drop_index(
        "uq_attention_assessment_active_signal",
        table_name="attention_assessments",
    )
    op.drop_table("attention_assessments")
