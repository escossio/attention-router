"""Platform Evolution Wave C: diagnosis, verification and governance.

Revision ID: 0018_platform_evolution_wave_c
Revises: 0017_platform_evolution_wave_b
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0018_platform_evolution_wave_c"
down_revision = "0017_platform_evolution_wave_b"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    j = _json_type()

    op.create_table(
        "diagnosis_candidates",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("finding_id", sa.String(64), sa.ForeignKey("findings.id"), nullable=False),
        sa.Column("engine", sa.String(120), nullable=False),
        sa.Column("source", sa.String(120), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("supporting_evidence_ids", j, nullable=False),
        sa.Column("contradicting_evidence_ids", j, nullable=False),
        sa.Column("alternatives", j, nullable=False),
        sa.Column("missing_information", j, nullable=False),
        sa.Column("suspected_component", sa.String(160)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("provenance", j, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "supersedes_diagnosis_id",
            sa.String(64),
            sa.ForeignKey("diagnosis_candidates.id"),
        ),
        sa.CheckConstraint(
            "confidence >= 0 and confidence <= 1",
            name="ck_diagnosis_candidate_confidence",
        ),
    )
    op.create_index(
        "ix_diagnosis_candidate_finding_created",
        "diagnosis_candidates",
        ["tenant_id", "finding_id", "created_at"],
    )

    op.create_table(
        "remediation_proposals",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "diagnosis_candidate_id",
            sa.String(64),
            sa.ForeignKey("diagnosis_candidates.id"),
            nullable=False,
        ),
        sa.Column("scope", j, nullable=False),
        sa.Column("affected_components", j, nullable=False),
        sa.Column("change_summary", sa.Text(), nullable=False),
        sa.Column("risk_ids", j, nullable=False),
        sa.Column("rollback_requirements", j, nullable=False),
        sa.Column("required_tests", j, nullable=False),
        sa.Column("required_invariants", j, nullable=False),
        sa.Column("required_evidence", j, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("author_type", sa.String(40), nullable=False),
        sa.Column("author_reference", sa.String(120), nullable=False),
        sa.Column("provenance", j, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "supersedes_proposal_id",
            sa.String(64),
            sa.ForeignKey("remediation_proposals.id"),
        ),
    )
    op.create_index(
        "ix_remediation_proposal_diagnosis_created",
        "remediation_proposals",
        ["tenant_id", "diagnosis_candidate_id", "created_at"],
    )

    op.create_table(
        "patch_candidates",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "remediation_proposal_id",
            sa.String(64),
            sa.ForeignKey("remediation_proposals.id"),
        ),
        sa.Column("base_sha", sa.String(128), nullable=False),
        sa.Column("candidate_sha", sa.String(128), nullable=False),
        sa.Column("branch_reference", sa.String(240), nullable=False),
        sa.Column("worktree_reference", sa.String(320), nullable=False),
        sa.Column("artifact_references", j, nullable=False),
        sa.Column("provenance", j, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("base_sha <> candidate_sha", name="ck_patch_candidate_distinct_sha"),
        sa.UniqueConstraint(
            "tenant_id", "candidate_sha", name="uq_patch_candidate_tenant_sha"
        ),
    )
    op.create_index(
        "ix_patch_candidate_status_created",
        "patch_candidates",
        ["tenant_id", "status", "created_at"],
    )

    op.create_table(
        "verification_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "patch_candidate_id",
            sa.String(64),
            sa.ForeignKey("patch_candidates.id"),
            nullable=False,
        ),
        sa.Column("candidate_sha_snapshot", sa.String(128), nullable=False),
        sa.Column("environment_identity", sa.String(160), nullable=False),
        sa.Column("test_results", j, nullable=False),
        sa.Column("assertion_result_ids", j, nullable=False),
        sa.Column("invariant_result_ids", j, nullable=False),
        sa.Column("differential_results", j, nullable=False),
        sa.Column("rollback_evidence", j, nullable=False),
        sa.Column("artifact_references", j, nullable=False),
        sa.Column("secret_scan_status", sa.String(32), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("provenance", j, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_verification_run_candidate_created",
        "verification_runs",
        ["tenant_id", "patch_candidate_id", "created_at"],
    )

    op.create_table(
        "promotion_decisions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "patch_candidate_id",
            sa.String(64),
            sa.ForeignKey("patch_candidates.id"),
            nullable=False,
        ),
        sa.Column(
            "verification_run_id", sa.String(64), sa.ForeignKey("verification_runs.id")
        ),
        sa.Column("promotion_request_id", sa.String(64)),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("authority_type", sa.String(40), nullable=False),
        sa.Column("authority_reference", sa.String(120)),
        sa.Column("evidence_coverage", j, nullable=False),
        sa.Column("blocking_finding_snapshot", j, nullable=False),
        sa.Column("risk_disposition", j, nullable=False),
        sa.Column("rollback_reference", sa.String(320)),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("required_source_sha", sa.String(128), nullable=False),
        sa.Column("required_runtime_sha", sa.String(128)),
        sa.Column("required_schema_revision", sa.String(128), nullable=False),
        sa.Column("provenance", j, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision in ('PROMOTION_REQUIRED','APPROVED','REJECTED','CANCELLED')",
            name="ck_promotion_decision_value",
        ),
        sa.CheckConstraint(
            "decision <> 'APPROVED' or "
            "(authority_type in ('HUMAN_OWNER','HUMAN_OPERATOR') "
            "and authority_reference is not null)",
            name="ck_promotion_approval_human_authority",
        ),
        sa.CheckConstraint(
            "(decision = 'PROMOTION_REQUIRED' and promotion_request_id is null) or "
            "(decision <> 'PROMOTION_REQUIRED' and promotion_request_id is not null)",
            name="ck_promotion_terminal_request_link",
        ),
        sa.UniqueConstraint(
            "promotion_request_id",
            name="uq_promotion_decision_terminal_request",
        ),
    )
    op.create_foreign_key(
        "fk_promotion_decision_request",
        "promotion_decisions",
        "promotion_decisions",
        ["promotion_request_id"],
        ["id"],
    )
    op.create_index(
        "ix_promotion_decision_candidate_created",
        "promotion_decisions",
        ["tenant_id", "patch_candidate_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_promotion_decision_request",
        "promotion_decisions",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_promotion_decision_candidate_created", table_name="promotion_decisions"
    )
    op.drop_table("promotion_decisions")
    op.drop_index("ix_verification_run_candidate_created", table_name="verification_runs")
    op.drop_table("verification_runs")
    op.drop_index("ix_patch_candidate_status_created", table_name="patch_candidates")
    op.drop_table("patch_candidates")
    op.drop_index(
        "ix_remediation_proposal_diagnosis_created", table_name="remediation_proposals"
    )
    op.drop_table("remediation_proposals")
    op.drop_index(
        "ix_diagnosis_candidate_finding_created", table_name="diagnosis_candidates"
    )
    op.drop_table("diagnosis_candidates")
