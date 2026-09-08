"""Platform Evolution Wave A: operations, readiness, findings and evidence.

Revision ID: 0016_platform_evolution_wave_a
Revises: 0015_capability_pack_v1
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0016_platform_evolution_wave_a"
down_revision = "0015_capability_pack_v1"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    j = _json_type()

    op.create_table(
        "dependency_definitions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("canonical_key", sa.String(160), nullable=False),
        sa.Column("dependency_type", sa.String(80), nullable=False),
        sa.Column("owner_module", sa.String(160), nullable=False),
        sa.Column("criticality", sa.String(40), nullable=False),
        sa.Column("authoritative_source", sa.String(120), nullable=False),
        sa.Column("health_source", sa.String(160)),
        sa.Column("freshness_source", sa.String(160)),
        sa.Column("sanitized_metadata", j, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("provenance_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_revision", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "canonical_key", name="uq_dependency_definition_tenant_key"
        ),
    )
    op.create_index(
        "ix_dependency_definition_owner_type_active",
        "dependency_definitions",
        ["tenant_id", "owner_module", "dependency_type", "is_active"],
    )

    op.create_table(
        "dependency_edges",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "upstream_dependency_id",
            sa.String(64),
            sa.ForeignKey("dependency_definitions.id"),
            nullable=False,
        ),
        sa.Column(
            "downstream_dependency_id",
            sa.String(64),
            sa.ForeignKey("dependency_definitions.id"),
            nullable=False,
        ),
        sa.Column("relation_type", sa.String(24), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("provenance", j, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "upstream_dependency_id <> downstream_dependency_id",
            name="ck_dependency_edge_not_self",
        ),
        sa.CheckConstraint(
            "relation_type in ('MANDATORY','ADVISORY')",
            name="ck_dependency_edge_relation_type",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "upstream_dependency_id",
            "downstream_dependency_id",
            "relation_type",
            name="uq_dependency_edge_tenant_relation",
        ),
    )
    op.create_index(
        "ix_dependency_edge_upstream",
        "dependency_edges",
        ["tenant_id", "upstream_dependency_id"],
    )
    op.create_index(
        "ix_dependency_edge_downstream",
        "dependency_edges",
        ["tenant_id", "downstream_dependency_id"],
    )

    op.create_table(
        "operational_observations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("source", sa.String(160), nullable=False),
        sa.Column("source_type", sa.String(80), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("freshness_expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "dependency_id",
            sa.String(64),
            sa.ForeignKey("dependency_definitions.id"),
        ),
        sa.Column("component_key", sa.String(160)),
        sa.Column(
            "lineage_classification",
            sa.String(32),
            nullable=False,
            server_default="HISTORICAL_UNKNOWN",
        ),
        # The Wave B migration adds the FK after scenario_runs exists.
        sa.Column("scenario_run_id", sa.String(64)),
        sa.Column("correlation_id", sa.String(64)),
        sa.Column("reason_code", sa.String(120), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("sanitized_metadata", j, nullable=False),
        sa.Column("source_revision", sa.String(128)),
        sa.Column("runtime_revision", sa.String(128)),
        sa.Column("schema_revision", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "dependency_id is not null or component_key is not null",
            name="ck_operational_observation_subject",
        ),
    )
    op.create_index(
        "ix_operational_observation_subject_time",
        "operational_observations",
        ["tenant_id", "component_key", "observed_at"],
    )
    op.create_index(
        "ix_operational_observation_dependency_time",
        "operational_observations",
        ["tenant_id", "dependency_id", "observed_at"],
    )
    op.create_index(
        "ix_operational_observation_correlation",
        "operational_observations",
        ["tenant_id", "correlation_id"],
    )

    op.create_table(
        "readiness_results",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("dimension", sa.String(40), nullable=False),
        sa.Column("subject_type", sa.String(40), nullable=False),
        sa.Column("subject_key", sa.String(160), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reason_codes", j, nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_fresh_until", sa.DateTime(timezone=True)),
        sa.Column("source_references", j, nullable=False),
        sa.Column("blocker_references", j, nullable=False),
        sa.Column("required_dependency_ids", j, nullable=False),
        sa.Column("provenance", j, nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "dimension in ('COMPONENT_HEALTH','DOMAIN_READINESS','EVIDENCE_READINESS')",
            name="ck_readiness_result_dimension",
        ),
        sa.CheckConstraint(
            "state in ('READY','DEGRADED','BLOCKED','UNKNOWN','STALE','NOT_READY')",
            name="ck_readiness_result_state",
        ),
    )
    op.create_index(
        "ix_readiness_subject_evaluated",
        "readiness_results",
        ["tenant_id", "dimension", "subject_type", "subject_key", "evaluated_at"],
    )
    op.create_index(
        "uq_readiness_current_subject",
        "readiness_results",
        ["tenant_id", "dimension", "subject_type", "subject_key"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        sqlite_where=sa.text("is_current = 1"),
    )

    op.create_table(
        "findings",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("fingerprint_version", sa.Integer(), nullable=False),
        sa.Column("active_scope", sa.String(160), nullable=False),
        sa.Column("category", sa.String(80), nullable=False),
        sa.Column("severity", sa.String(24), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("lineage_classification", sa.String(32), nullable=False),
        sa.Column("owner_reference", sa.String(120)),
        sa.Column("acknowledged_by", sa.String(120)),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("suppression_scope", sa.String(160)),
        sa.Column("suppression_reason", sa.String(320)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution_reason", sa.String(320)),
        sa.Column("provenance", j, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("fingerprint_version > 0", name="ck_finding_fingerprint_version"),
        sa.CheckConstraint("occurrence_count > 0", name="ck_finding_occurrence_count"),
        sa.CheckConstraint("last_seen_at >= first_seen_at", name="ck_finding_seen_order"),
    )
    op.create_index(
        "ix_findings_tenant_status_severity_seen",
        "findings",
        ["tenant_id", "status", "severity", "last_seen_at"],
    )
    op.create_index(
        "uq_findings_active_fingerprint",
        "findings",
        ["tenant_id", "fingerprint_version", "fingerprint", "active_scope"],
        unique=True,
        postgresql_where=sa.text("status in ('NEW','ACTIVE','ACKNOWLEDGED')"),
        sqlite_where=sa.text("status in ('NEW','ACTIVE','ACKNOWLEDGED')"),
    )

    op.create_table(
        "finding_occurrences",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("finding_id", sa.String(64), sa.ForeignKey("findings.id"), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("component_key", sa.String(160), nullable=False),
        # The Wave B migration adds the FK after scenario_runs exists.
        sa.Column("scenario_run_id", sa.String(64)),
        sa.Column("correlation_id", sa.String(64)),
        sa.Column("reason_code", sa.String(120), nullable=False),
        sa.Column("provenance", j, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_finding_occurrence_finding_time",
        "finding_occurrences",
        ["finding_id", "observed_at"],
    )
    op.create_index(
        "ix_finding_occurrence_correlation",
        "finding_occurrences",
        ["tenant_id", "correlation_id"],
    )

    op.create_table(
        "evidence_references",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("evidence_type", sa.String(64), nullable=False),
        sa.Column("finding_id", sa.String(64), sa.ForeignKey("findings.id")),
        sa.Column(
            "finding_occurrence_id",
            sa.String(64),
            sa.ForeignKey("finding_occurrences.id"),
        ),
        sa.Column("internal_entity_type", sa.String(80)),
        sa.Column("internal_entity_id", sa.String(128)),
        sa.Column("external_reference", sa.String(320)),
        sa.Column("artifact_reference", sa.String(320)),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("source_sha", sa.String(128)),
        sa.Column("sanitized_metadata", j, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "internal_entity_id is not null or external_reference is not null "
            "or artifact_reference is not null or trace_id is not null "
            "or finding_id is not null or finding_occurrence_id is not null",
            name="ck_evidence_reference_target",
        ),
        sa.CheckConstraint(
            "(internal_entity_type is null) = (internal_entity_id is null)",
            name="ck_evidence_reference_internal_pair",
        ),
    )
    op.create_index(
        "ix_evidence_reference_internal",
        "evidence_references",
        ["tenant_id", "internal_entity_type", "internal_entity_id"],
    )
    op.create_index(
        "ix_evidence_reference_trace_source",
        "evidence_references",
        ["tenant_id", "trace_id", "source_sha"],
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_reference_trace_source", table_name="evidence_references")
    op.drop_index("ix_evidence_reference_internal", table_name="evidence_references")
    op.drop_table("evidence_references")
    op.drop_index("ix_finding_occurrence_correlation", table_name="finding_occurrences")
    op.drop_index("ix_finding_occurrence_finding_time", table_name="finding_occurrences")
    op.drop_table("finding_occurrences")
    op.drop_index("uq_findings_active_fingerprint", table_name="findings")
    op.drop_index("ix_findings_tenant_status_severity_seen", table_name="findings")
    op.drop_table("findings")
    op.drop_index("uq_readiness_current_subject", table_name="readiness_results")
    op.drop_index("ix_readiness_subject_evaluated", table_name="readiness_results")
    op.drop_table("readiness_results")
    op.drop_index(
        "ix_operational_observation_correlation", table_name="operational_observations"
    )
    op.drop_index(
        "ix_operational_observation_dependency_time", table_name="operational_observations"
    )
    op.drop_index(
        "ix_operational_observation_subject_time", table_name="operational_observations"
    )
    op.drop_table("operational_observations")
    op.drop_index("ix_dependency_edge_downstream", table_name="dependency_edges")
    op.drop_index("ix_dependency_edge_upstream", table_name="dependency_edges")
    op.drop_table("dependency_edges")
    op.drop_index(
        "ix_dependency_definition_owner_type_active", table_name="dependency_definitions"
    )
    op.drop_table("dependency_definitions")
