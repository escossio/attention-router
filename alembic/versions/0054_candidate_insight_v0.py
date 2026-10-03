"""Personal Context V2E Candidate Insight.

Revision ID: 0054_candidate_insight_v0
Revises: 0053_semantic_episode_v0
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0054_candidate_insight_v0"
down_revision = "0053_semantic_episode_v0"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    json_type = _json_type()

    op.create_table(
        "candidate_insights",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.id", name="fk_candidate_insight_tenant"),
            nullable=False,
        ),
        sa.Column("semantic_key", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("insight_type", sa.String(48), nullable=False),
        sa.Column("subject_type", sa.String(40), nullable=False),
        sa.Column("subject_id", sa.String(120), nullable=False),
        sa.Column("predicate", sa.String(160), nullable=False),
        sa.Column("proposed_value", json_type, nullable=False),
        sa.Column("source_engine", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("sensitivity_class", sa.String(20), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("contradiction_refs", json_type, nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column(
            "supersedes_insight_id",
            sa.String(64),
            sa.ForeignKey(
                "candidate_insights.id",
                name="fk_candidate_insight_supersedes",
            ),
            nullable=True,
        ),
        sa.Column("decision_kind", sa.String(64), nullable=True),
        sa.Column("decision_actor_key", sa.String(120), nullable=True),
        sa.Column("decision_ref", sa.String(240), nullable=True),
        sa.Column("provenance", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_candidate_insight_tenant_idempotency",
        ),
        sa.CheckConstraint(
            "insight_type IN "
            "('CLAIM_PROPOSAL','RELATIONSHIP_PROPOSAL','STATE_PROPOSAL')",
            name="ck_candidate_insight_type",
        ),
        sa.CheckConstraint(
            "subject_type IN "
            "('ACTOR','PERSON','RESOURCE','RELATIONSHIP')",
            name="ck_candidate_insight_subject_type",
        ),
        sa.CheckConstraint(
            "source_engine IN ('RULE','LLM','EMBEDDING','GNN')",
            name="ck_candidate_insight_source_engine",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_candidate_insight_confidence",
        ),
        sa.CheckConstraint(
            "sensitivity_class IN ('NORMAL','PRIVATE','SECRET')",
            name="ck_candidate_insight_sensitivity",
        ),
        sa.CheckConstraint(
            "state IN "
            "('PROPOSED','ADMITTED','REJECTED','NEEDS_REVIEW','SUPERSEDED')",
            name="ck_candidate_insight_state",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_from IS NULL "
            "OR valid_until >= valid_from",
            name="ck_candidate_insight_temporal_order",
        ),
    )
    op.create_index(
        "ix_candidate_insight_tenant_state",
        "candidate_insights",
        ["tenant_id", "state", "updated_at"],
    )
    op.create_index(
        "ix_candidate_insight_subject",
        "candidate_insights",
        ["tenant_id", "subject_type", "subject_id", "predicate"],
    )
    op.create_index(
        "ix_candidate_insight_semantic_key",
        "candidate_insights",
        ["tenant_id", "semantic_key", "updated_at"],
    )

    op.create_table(
        "candidate_insight_evidence",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey(
                "tenants.id",
                name="fk_candidate_insight_evidence_tenant",
            ),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.String(64),
            sa.ForeignKey(
                "candidate_insights.id",
                name="fk_candidate_insight_evidence_candidate",
            ),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("evidence_type", sa.String(40), nullable=False),
        sa.Column("source_ref", sa.String(240), nullable=False),
        sa.Column("independence_key", sa.String(160), nullable=False),
        sa.Column("evidence_role", sa.String(24), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("sensitivity_class", sa.String(20), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_candidate_insight_evidence_idempotency",
        ),
        sa.UniqueConstraint(
            "candidate_id",
            "evidence_type",
            "source_ref",
            "evidence_role",
            name="uq_candidate_insight_evidence_source",
        ),
        sa.CheckConstraint(
            "evidence_type IN "
            "('SEMANTIC_EPISODE','TIMELINE_EVENT','CONVERSATION_MESSAGE')",
            name="ck_candidate_insight_evidence_type",
        ),
        sa.CheckConstraint(
            "evidence_role IN ('SUPPORT','CONTRADICTION')",
            name="ck_candidate_insight_evidence_role",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_candidate_insight_evidence_confidence",
        ),
        sa.CheckConstraint(
            "sensitivity_class IN ('NORMAL','PRIVATE','SECRET')",
            name="ck_candidate_insight_evidence_sensitivity",
        ),
    )
    op.create_index(
        "ix_candidate_insight_evidence_candidate",
        "candidate_insight_evidence",
        ["candidate_id", "observed_at"],
    )
    op.create_index(
        "ix_candidate_insight_evidence_source",
        "candidate_insight_evidence",
        ["tenant_id", "evidence_type", "source_ref"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("candidate_insight_evidence", "candidate_insights"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(
                "CANDIDATE_INSIGHT_DOWNGRADE_REQUIRES_DATA_EXPORT"
            )

    op.drop_index(
        "ix_candidate_insight_evidence_source",
        table_name="candidate_insight_evidence",
    )
    op.drop_index(
        "ix_candidate_insight_evidence_candidate",
        table_name="candidate_insight_evidence",
    )
    op.drop_table("candidate_insight_evidence")

    op.drop_index(
        "ix_candidate_insight_semantic_key",
        table_name="candidate_insights",
    )
    op.drop_index(
        "ix_candidate_insight_subject",
        table_name="candidate_insights",
    )
    op.drop_index(
        "ix_candidate_insight_tenant_state",
        table_name="candidate_insights",
    )
    op.drop_table("candidate_insights")
