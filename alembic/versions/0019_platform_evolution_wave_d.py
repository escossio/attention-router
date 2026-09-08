"""Platform Evolution Wave D: safe compatibility tightening.

Revision ID: 0019_platform_evolution_wave_d
Revises: 0018_platform_evolution_wave_c
"""

from alembic import op


revision = "0019_platform_evolution_wave_d"
down_revision = "0018_platform_evolution_wave_c"
branch_labels = None
depends_on = None


_CHECKS = (
    (
        "inbound_events",
        "ck_inbound_event_lineage_classification",
        "lineage_classification in "
        "('HISTORICAL_UNKNOWN','UNKNOWN','ORGANIC','SYNTHETIC')",
    ),
    (
        "canonical_events",
        "ck_canonical_event_lineage_classification",
        "lineage_classification in "
        "('HISTORICAL_UNKNOWN','UNKNOWN','ORGANIC','SYNTHETIC')",
    ),
    (
        "operational_observations",
        "ck_operational_observation_lineage",
        "lineage_classification in "
        "('HISTORICAL_UNKNOWN','UNKNOWN','ORGANIC','SYNTHETIC')",
    ),
    (
        "findings",
        "ck_finding_status",
        "status in ('NEW','ACTIVE','ACKNOWLEDGED','RESOLVED','SUPPRESSED','EXPECTED')",
    ),
    (
        "findings",
        "ck_finding_severity",
        "severity in ('CRITICAL','HIGH','MEDIUM','LOW')",
    ),
    (
        "findings",
        "ck_finding_lineage_classification",
        "lineage_classification in "
        "('HISTORICAL_UNKNOWN','UNKNOWN','ORGANIC','SYNTHETIC','MIXED')",
    ),
    (
        "scenario_versions",
        "ck_scenario_version_immutable",
        "is_immutable",
    ),
    (
        "scenario_runs",
        "ck_scenario_run_status",
        "status in ('CREATED','VALIDATING','NOT_READY','ARMED','RUNNING',"
        "'VERIFYING','PASSED','FAILED','ABORTED','EXPIRED')",
    ),
    (
        "scenario_runs",
        "ck_scenario_run_cleanup_state",
        "cleanup_state in ('PENDING','RUNNING','COMPLETE','FAILED','NOT_REQUIRED')",
    ),
    (
        "scenario_runs",
        "ck_scenario_run_terminal_completed",
        "status not in ('PASSED','FAILED','ABORTED','EXPIRED') "
        "or completed_at is not null",
    ),
    (
        "scenario_step_runs",
        "ck_scenario_step_status",
        "status in ('PENDING','RUNNING','VERIFYING','PASSED','FAILED','SKIPPED',"
        "'ABORTED','EXPIRED','RECONCILIATION_REQUIRED')",
    ),
    (
        "scenario_step_runs",
        "ck_scenario_step_cleanup_state",
        "cleanup_state in ('PENDING','RUNNING','COMPLETE','FAILED','NOT_REQUIRED')",
    ),
    (
        "effect_budgets",
        "ck_effect_budget_status",
        "status in ('AVAILABLE','RESERVED','CONSUMED','RELEASED','CANCELLED')",
    ),
    (
        "effect_budgets",
        "ck_effect_budget_capacity",
        "reserved_count <= stimulus_limit + system_effect_limit "
        "and consumed_count <= stimulus_limit + system_effect_limit",
    ),
    ("effect_budgets", "ck_effect_budget_version", "version > 0"),
    (
        "execution_leases",
        "ck_execution_lease_status",
        "status in ('AVAILABLE','CLAIMED','CONSUMED','EXPIRED','CANCELLED','FAILED')",
    ),
    ("execution_leases", "ck_execution_lease_version", "version > 0"),
    (
        "effect_consumptions",
        "ck_effect_consumption_state",
        "state in ('RESERVED','CONSUMED','RELEASED','CANCELLED')",
    ),
)


def upgrade() -> None:
    # SQLite cannot add constraints without rebuilding tables. Runtime is
    # PostgreSQL; SQLite metadata-based tests receive the same checks from ORM.
    if op.get_bind().dialect.name == "sqlite":
        return
    op.create_index(
        "uq_effect_consumption_execution_intent",
        "effect_consumptions",
        ["execution_intent_id"],
        unique=True,
        postgresql_where="execution_intent_id is not null",
    )
    op.create_index(
        "uq_effect_consumption_outbox_message",
        "effect_consumptions",
        ["outbox_message_id"],
        unique=True,
        postgresql_where="outbox_message_id is not null",
    )
    for table, name, condition in _CHECKS:
        op.create_check_constraint(name, table, condition)


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    for table, name, _condition in reversed(_CHECKS):
        op.drop_constraint(name, table, type_="check")
    op.drop_index("uq_effect_consumption_outbox_message", table_name="effect_consumptions")
    op.drop_index("uq_effect_consumption_execution_intent", table_name="effect_consumptions")
