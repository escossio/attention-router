"""Platform Evolution Wave B: lineage, scenarios and execution safety.

Revision ID: 0017_platform_evolution_wave_b
Revises: 0016_platform_evolution_wave_a
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0017_platform_evolution_wave_b"
down_revision = "0016_platform_evolution_wave_a"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    j = _json_type()
    dialect = op.get_bind().dialect.name

    op.add_column(
        "inbound_events",
        sa.Column(
            "lineage_classification",
            sa.String(32),
            nullable=False,
            server_default="HISTORICAL_UNKNOWN",
        ),
    )
    op.add_column("inbound_events", sa.Column("scenario_run_id", sa.String(64)))
    op.add_column("inbound_events", sa.Column("scenario_step_run_id", sa.String(64)))

    op.add_column(
        "canonical_events",
        sa.Column(
            "lineage_classification",
            sa.String(32),
            nullable=False,
            server_default="HISTORICAL_UNKNOWN",
        ),
    )
    op.add_column("canonical_events", sa.Column("scenario_run_id", sa.String(64)))
    op.add_column("canonical_events", sa.Column("scenario_step_run_id", sa.String(64)))

    op.create_table(
        "scenario_definitions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("scenario_key", sa.String(120), nullable=False),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "scenario_key", name="uq_scenario_definition_tenant_key"
        ),
    )

    op.create_table(
        "scenario_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "scenario_definition_id",
            sa.String(64),
            sa.ForeignKey("scenario_definitions.id"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(40), nullable=False),
        sa.Column("manifest_source_path", sa.String(320), nullable=False),
        sa.Column("content_hash", sa.String(128), nullable=False),
        sa.Column("requirements_covered", j, nullable=False),
        sa.Column("risk_ids", j, nullable=False),
        sa.Column("config_keys", j, nullable=False),
        sa.Column("risk_classification", sa.String(40), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source_sha", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_immutable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.CheckConstraint("version > 0", name="ck_scenario_version_positive"),
        sa.UniqueConstraint(
            "scenario_definition_id",
            "version",
            name="uq_scenario_version_definition_version",
        ),
        sa.UniqueConstraint(
            "scenario_definition_id",
            "content_hash",
            name="uq_scenario_version_definition_hash",
        ),
    )

    op.create_table(
        "scenario_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "scenario_version_id",
            sa.String(64),
            sa.ForeignKey("scenario_versions.id"),
            nullable=False,
        ),
        sa.Column(
            "synthetic_actor_binding_id",
            sa.String(64),
            sa.ForeignKey("actor_bindings.id"),
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("root_correlation_id", sa.String(64), nullable=False),
        sa.Column("source_sha", sa.String(128), nullable=False),
        sa.Column("runtime_sha", sa.String(128)),
        sa.Column("schema_revision", sa.String(128), nullable=False),
        sa.Column("driver_revision", sa.String(128)),
        sa.Column(
            "readiness_result_id",
            sa.String(64),
            sa.ForeignKey("readiness_results.id"),
        ),
        # Added as a named FK after effect_budgets exists.
        sa.Column("effect_budget_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True)),
        sa.Column("armed_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("verifying_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_reason", sa.String(320)),
        sa.Column("cleanup_state", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "root_correlation_id", name="uq_scenario_run_tenant_correlation"
        ),
    )
    op.create_index(
        "ix_scenario_run_tenant_status_created",
        "scenario_runs",
        ["tenant_id", "status", "created_at"],
    )

    op.create_table(
        "scenario_step_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "scenario_run_id", sa.String(64), sa.ForeignKey("scenario_runs.id"), nullable=False
        ),
        sa.Column("step_key", sa.String(120), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("step_kind", sa.String(40), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("idempotency_key", sa.String(180), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        # Added as a named FK after execution_leases exists.
        sa.Column("execution_lease_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_reason", sa.String(320)),
        sa.Column("cleanup_state", sa.String(32), nullable=False, server_default="PENDING"),
        sa.CheckConstraint("step_order >= 0", name="ck_scenario_step_order"),
        sa.CheckConstraint(
            "attempt >= 0 and attempt <= max_attempts and max_attempts > 0",
            name="ck_scenario_step_attempts",
        ),
        sa.UniqueConstraint("scenario_run_id", "step_key", name="uq_scenario_step_run_key"),
        sa.UniqueConstraint(
            "scenario_run_id", "step_order", name="uq_scenario_step_run_order"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "scenario_run_id",
            "idempotency_key",
            name="uq_scenario_step_run_idempotency",
        ),
    )
    op.create_index(
        "ix_scenario_step_run_status",
        "scenario_step_runs",
        ["tenant_id", "scenario_run_id", "status"],
    )

    op.create_table(
        "effect_budgets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "scenario_run_id", sa.String(64), sa.ForeignKey("scenario_runs.id"), nullable=False
        ),
        sa.Column("effect_type", sa.String(80), nullable=False),
        sa.Column("target_scope", sa.String(240), nullable=False),
        sa.Column("stimulus_limit", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("system_effect_limit", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consumed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="AVAILABLE"),
        sa.Column("provenance", j, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "stimulus_limit >= 0 and system_effect_limit >= 0",
            name="ck_effect_budget_limits",
        ),
        sa.CheckConstraint(
            "reserved_count >= 0 and consumed_count >= 0", name="ck_effect_budget_counts"
        ),
        sa.CheckConstraint("valid_until > valid_from", name="ck_effect_budget_validity"),
        sa.UniqueConstraint(
            "tenant_id",
            "scenario_run_id",
            "effect_type",
            "target_scope",
            name="uq_effect_budget_run_effect_target",
        ),
    )
    op.create_index(
        "ix_effect_budget_run_status",
        "effect_budgets",
        ["tenant_id", "scenario_run_id", "status"],
    )

    op.create_table(
        "execution_leases",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("lease_type", sa.String(80), nullable=False),
        sa.Column("purpose", sa.String(160), nullable=False),
        sa.Column("scope_key", sa.String(180), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("scenario_run_id", sa.String(64), sa.ForeignKey("scenario_runs.id")),
        sa.Column(
            "scenario_step_run_id", sa.String(64), sa.ForeignKey("scenario_step_runs.id")
        ),
        sa.Column("claimant_id", sa.String(120)),
        sa.Column("status", sa.String(32), nullable=False, server_default="AVAILABLE"),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("claim_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_claims", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("claimed_event_id", sa.String(64), sa.ForeignKey("inbound_events.id")),
        sa.Column("logical_execution_id", sa.String(120)),
        sa.Column("effect_budget_id", sa.String(64), sa.ForeignKey("effect_budgets.id")),
        sa.Column("idempotency_key", sa.String(180), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "claim_count >= 0 and max_claims > 0 and claim_count <= max_claims",
            name="ck_execution_lease_claim_count",
        ),
        sa.CheckConstraint("expires_at > not_before", name="ck_execution_lease_validity"),
        sa.UniqueConstraint(
            "tenant_id",
            "lease_type",
            "idempotency_key",
            name="uq_execution_lease_tenant_type_idempotency",
        ),
    )
    op.create_index(
        "uq_execution_lease_active_scope",
        "execution_leases",
        ["tenant_id", "lease_type", "scope_key"],
        unique=True,
        postgresql_where=sa.text("status in ('AVAILABLE','CLAIMED')"),
        sqlite_where=sa.text("status in ('AVAILABLE','CLAIMED')"),
    )
    op.create_index(
        "ix_execution_lease_claim",
        "execution_leases",
        ["tenant_id", "lease_type", "status", "expires_at"],
    )

    op.create_table(
        "effect_consumptions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "effect_budget_id",
            sa.String(64),
            sa.ForeignKey("effect_budgets.id"),
            nullable=False,
        ),
        sa.Column("logical_effect_id", sa.String(160), nullable=False),
        sa.Column("direction", sa.String(24), nullable=False),
        sa.Column("target_scope", sa.String(240), nullable=False),
        sa.Column(
            "execution_lease_id",
            sa.String(64),
            sa.ForeignKey("execution_leases.id"),
            nullable=False,
        ),
        sa.Column(
            "execution_intent_id", sa.String(64), sa.ForeignKey("agent_execution_intents.id")
        ),
        sa.Column("outbox_message_id", sa.String(64), sa.ForeignKey("outbox_messages.id")),
        sa.Column("idempotency_key", sa.String(180), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="RESERVED"),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("provenance", j, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "direction in ('STIMULUS','SYSTEM')", name="ck_effect_consumption_direction"
        ),
        sa.UniqueConstraint(
            "effect_budget_id",
            "logical_effect_id",
            name="uq_effect_consumption_budget_logical_effect",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_effect_consumption_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_effect_consumption_budget_state",
        "effect_consumptions",
        ["effect_budget_id", "state"],
    )

    op.create_table(
        "assertion_results",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "scenario_run_id", sa.String(64), sa.ForeignKey("scenario_runs.id"), nullable=False
        ),
        sa.Column(
            "scenario_step_run_id", sa.String(64), sa.ForeignKey("scenario_step_runs.id")
        ),
        sa.Column("assertion_id", sa.String(120), nullable=False),
        sa.Column("assertion_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("expected_property", sa.Text(), nullable=False),
        sa.Column("observed_summary", sa.Text()),
        sa.Column("evaluator", sa.String(120), nullable=False),
        sa.Column("blocking", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("evidence_reference_ids", j, nullable=False),
        sa.Column("finding_id", sa.String(64), sa.ForeignKey("findings.id")),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance", j, nullable=False),
        sa.CheckConstraint(
            "result in ('PASS','FAIL','UNKNOWN','NOT_EVALUATED')",
            name="ck_assertion_result_value",
        ),
        sa.UniqueConstraint(
            "scenario_run_id",
            "assertion_id",
            "assertion_version",
            "attempt",
            name="uq_assertion_result_run_identity",
        ),
    )
    op.create_index(
        "ix_assertion_result_run_result",
        "assertion_results",
        ["tenant_id", "scenario_run_id", "result"],
    )

    op.create_table(
        "invariant_results",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "scenario_run_id", sa.String(64), sa.ForeignKey("scenario_runs.id"), nullable=False
        ),
        sa.Column(
            "scenario_step_run_id", sa.String(64), sa.ForeignKey("scenario_step_runs.id")
        ),
        sa.Column("invariant_id", sa.String(120), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("enforcement_owner", sa.String(120), nullable=False),
        sa.Column("severity", sa.String(24), nullable=False),
        sa.Column("blocking", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("violation_reason", sa.String(320)),
        sa.Column("aborts_scenario", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("blocks_promotion", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("evidence_reference_ids", j, nullable=False),
        sa.Column("finding_id", sa.String(64), sa.ForeignKey("findings.id")),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance", j, nullable=False),
        sa.CheckConstraint(
            "result in ('PASS','FAIL','UNKNOWN','NOT_EVALUATED')",
            name="ck_invariant_result_value",
        ),
        sa.UniqueConstraint(
            "scenario_run_id",
            "invariant_id",
            "attempt",
            name="uq_invariant_result_run_identity",
        ),
    )
    op.create_index(
        "ix_invariant_result_run_result",
        "invariant_results",
        ["tenant_id", "scenario_run_id", "result"],
    )

    if dialect != "sqlite":
        op.create_foreign_key(
            "fk_scenario_runs_effect_budget",
            "scenario_runs",
            "effect_budgets",
            ["effect_budget_id"],
            ["id"],
        )
        op.create_foreign_key(
            "fk_scenario_step_runs_execution_lease",
            "scenario_step_runs",
            "execution_leases",
            ["execution_lease_id"],
            ["id"],
        )
        for table, column, target, name in (
            (
                "inbound_events",
                "scenario_run_id",
                "scenario_runs",
                "fk_inbound_events_scenario_run",
            ),
            (
                "inbound_events",
                "scenario_step_run_id",
                "scenario_step_runs",
                "fk_inbound_events_scenario_step_run",
            ),
            (
                "canonical_events",
                "scenario_run_id",
                "scenario_runs",
                "fk_canonical_events_scenario_run",
            ),
            (
                "canonical_events",
                "scenario_step_run_id",
                "scenario_step_runs",
                "fk_canonical_events_scenario_step_run",
            ),
            (
                "operational_observations",
                "scenario_run_id",
                "scenario_runs",
                "fk_operational_observations_scenario_run",
            ),
            (
                "finding_occurrences",
                "scenario_run_id",
                "scenario_runs",
                "fk_finding_occurrences_scenario_run",
            ),
        ):
            op.create_foreign_key(name, table, target, [column], ["id"])

    op.create_index(
        "ix_inbound_events_scenario_run",
        "inbound_events",
        ["tenant_id", "scenario_run_id"],
    )
    op.create_index(
        "ix_canonical_events_scenario_run",
        "canonical_events",
        ["tenant_id", "scenario_run_id"],
    )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name

    op.drop_index("ix_canonical_events_scenario_run", table_name="canonical_events")
    op.drop_index("ix_inbound_events_scenario_run", table_name="inbound_events")

    if dialect != "sqlite":
        for table, name in (
            ("finding_occurrences", "fk_finding_occurrences_scenario_run"),
            ("operational_observations", "fk_operational_observations_scenario_run"),
            ("canonical_events", "fk_canonical_events_scenario_step_run"),
            ("canonical_events", "fk_canonical_events_scenario_run"),
            ("inbound_events", "fk_inbound_events_scenario_step_run"),
            ("inbound_events", "fk_inbound_events_scenario_run"),
            ("scenario_step_runs", "fk_scenario_step_runs_execution_lease"),
            ("scenario_runs", "fk_scenario_runs_effect_budget"),
        ):
            op.drop_constraint(name, table, type_="foreignkey")

    op.drop_index("ix_invariant_result_run_result", table_name="invariant_results")
    op.drop_table("invariant_results")
    op.drop_index("ix_assertion_result_run_result", table_name="assertion_results")
    op.drop_table("assertion_results")
    op.drop_index(
        "ix_effect_consumption_budget_state", table_name="effect_consumptions"
    )
    op.drop_table("effect_consumptions")
    op.drop_index("ix_execution_lease_claim", table_name="execution_leases")
    op.drop_index("uq_execution_lease_active_scope", table_name="execution_leases")
    op.drop_table("execution_leases")
    op.drop_index("ix_effect_budget_run_status", table_name="effect_budgets")
    op.drop_table("effect_budgets")
    op.drop_index("ix_scenario_step_run_status", table_name="scenario_step_runs")
    op.drop_table("scenario_step_runs")
    op.drop_index("ix_scenario_run_tenant_status_created", table_name="scenario_runs")
    op.drop_table("scenario_runs")
    op.drop_table("scenario_versions")
    op.drop_table("scenario_definitions")

    op.drop_column("canonical_events", "scenario_step_run_id")
    op.drop_column("canonical_events", "scenario_run_id")
    op.drop_column("canonical_events", "lineage_classification")
    op.drop_column("inbound_events", "scenario_step_run_id")
    op.drop_column("inbound_events", "scenario_run_id")
    op.drop_column("inbound_events", "lineage_classification")
