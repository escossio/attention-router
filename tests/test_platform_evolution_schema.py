from alembic.config import Config
from alembic.script import ScriptDirectory

from attention_router.infrastructure.db import Base
from attention_router.infrastructure import artifact_models  # noqa: F401
from attention_router.infrastructure import models  # noqa: F401


PLATFORM_TABLES = {
    "dependency_definitions",
    "dependency_edges",
    "operational_observations",
    "readiness_results",
    "findings",
    "finding_occurrences",
    "evidence_references",
    "scenario_definitions",
    "scenario_versions",
    "scenario_runs",
    "scenario_step_runs",
    "effect_budgets",
    "execution_leases",
    "effect_consumptions",
    "assertion_results",
    "invariant_results",
    "diagnosis_candidates",
    "remediation_proposals",
    "patch_candidates",
    "verification_runs",
    "promotion_decisions",
}


def _constraint_names(table_name: str) -> set[str]:
    return {
        constraint.name
        for constraint in Base.metadata.tables[table_name].constraints
        if constraint.name
    }


def test_platform_evolution_tables_and_structural_lineage_are_registered():
    assert PLATFORM_TABLES <= set(Base.metadata.tables)
    assert len(PLATFORM_TABLES) == 21
    for table_name in ("inbound_events", "canonical_events"):
        columns = Base.metadata.tables[table_name].columns
        assert "lineage_classification" in columns
        assert "scenario_run_id" in columns
        assert "scenario_step_run_id" in columns
        assert str(columns["lineage_classification"].server_default.arg) == "HISTORICAL_UNKNOWN"


def test_artifact_plane_tables_are_registered():
    assert {"artifacts", "artifact_receipts"} <= set(Base.metadata.tables)
    assert "uq_artifact_tenant_sha256" in _constraint_names("artifacts")
    assert "uq_artifact_receipt_source" in _constraint_names("artifact_receipts")
    assert "fk_artifact_receipt_tenant_artifact" in _constraint_names("artifact_receipts")


def test_safety_constraints_and_partial_unique_indexes_are_registered():
    finding_indexes = {index.name: index for index in Base.metadata.tables["findings"].indexes}
    lease_indexes = {
        index.name: index for index in Base.metadata.tables["execution_leases"].indexes
    }
    assert finding_indexes["uq_findings_active_fingerprint"].unique
    assert lease_indexes["uq_execution_lease_active_scope"].unique

    consumption_constraints = _constraint_names("effect_consumptions")
    assert "uq_effect_consumption_budget_logical_effect" in consumption_constraints
    assert "uq_effect_consumption_tenant_idempotency" in consumption_constraints
    assert "ck_effect_consumption_state" in consumption_constraints

    promotion_constraints = _constraint_names("promotion_decisions")
    assert "ck_promotion_decision_value" in promotion_constraints
    assert "ck_promotion_approval_human_authority" in promotion_constraints


def test_scenario_and_budget_tightening_constraints_are_registered():
    assert "ck_scenario_version_immutable" in _constraint_names("scenario_versions")
    assert "ck_scenario_run_terminal_completed" in _constraint_names("scenario_runs")
    assert "ck_scenario_step_attempts" in _constraint_names("scenario_step_runs")
    assert "ck_effect_budget_capacity" in _constraint_names("effect_budgets")
    assert "ck_execution_lease_claim_count" in _constraint_names("execution_leases")


def test_platform_evolution_migration_waves_form_one_chain():
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    assert scripts.get_heads() == ["0036_artifact_registry_v0"]

    revisions = {revision.revision: revision for revision in scripts.walk_revisions()}
    assert revisions["0016_platform_evolution_wave_a"].down_revision == (
        "0015_capability_pack_v1"
    )
    assert revisions["0017_platform_evolution_wave_b"].down_revision == (
        "0016_platform_evolution_wave_a"
    )
    assert revisions["0018_platform_evolution_wave_c"].down_revision == (
        "0017_platform_evolution_wave_b"
    )
    assert revisions["0019_platform_evolution_wave_d"].down_revision == (
        "0018_platform_evolution_wave_c"
    )
    assert revisions["0024_execution_intent"].down_revision == "0023_human_execution_auth"
    assert revisions["0025_human_auth_execution_intent"].down_revision == "0024_execution_intent"
    assert revisions["0036_artifact_registry_v0"].down_revision == "0035_whatsapp_voice_media"
