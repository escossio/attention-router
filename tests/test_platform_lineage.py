import pytest
from types import SimpleNamespace

from attention_router.platform.lineage import (
    EventLineage,
    LineageClassification,
    LineageDenied,
    build_event_lineage,
    require_non_owner_authority,
    require_organic_write,
    require_scenario_actor_scope,
    require_structurally_synthetic_binding,
    require_unambiguous_stimulus_id,
    stamp_structural_lineage,
)


def test_synthetic_lineage_requires_structural_scenario_ids():
    with pytest.raises(ValueError, match="SYNTHETIC_SCENARIO_LINEAGE_REQUIRED"):
        build_event_lineage(
            tenant_id="tenant-a",
            actor_id="actor-synthetic",
            synthetic=True,
        )


def test_synthetic_actor_cannot_collide_with_owner_or_cross_tenant():
    lineage = build_event_lineage(
        tenant_id="tenant-a",
        actor_id="actor-owner",
        synthetic=True,
        scenario_id="SCN-PE-004",
        scenario_run_id="run-1",
        stimulus_id="stimulus-1",
    )
    with pytest.raises(LineageDenied, match="SYNTHETIC_ACTOR_OWNER_COLLISION"):
        require_scenario_actor_scope(
            lineage,
            scenario_tenant_id="tenant-a",
            owner_actor_id="actor-owner",
        )
    with pytest.raises(LineageDenied, match="CROSS_TENANT_SCENARIO_ACTOR"):
        require_scenario_actor_scope(
            lineage,
            scenario_tenant_id="tenant-b",
            owner_actor_id=None,
        )


def test_synthetic_actor_cannot_enter_owner_authority_paths():
    lineage = build_event_lineage(
        tenant_id="tenant-a",
        actor_id="actor-synthetic",
        synthetic=True,
        scenario_id="SCN-PE-004",
        scenario_run_id="run-1",
        stimulus_id="stimulus-1",
    )
    with pytest.raises(LineageDenied, match="SYNTHETIC_SELF_CHAT_OWNER_PATH_DENIED"):
        require_non_owner_authority(lineage, self_chat=True)
    with pytest.raises(LineageDenied, match="SYNTHETIC_OWNER_ONLY_GRANT_DENIED"):
        require_non_owner_authority(lineage, owner_only_grant=True)


@pytest.mark.parametrize(
    "classification,reason",
    [
        (LineageClassification.SYNTHETIC, "SYNTHETIC_MEMORY_PROMOTION_DENIED"),
        (
            LineageClassification.HISTORICAL_UNKNOWN,
            "UNKNOWN_LINEAGE_MEMORY_PROMOTION_DENIED",
        ),
    ],
)
def test_non_organic_lineage_fails_closed_for_memory(classification, reason):
    lineage = EventLineage(
        tenant_id="tenant-a",
        actor_id="actor-a",
        classification=classification,
        scenario_id="SCN-PE-005" if classification is LineageClassification.SYNTHETIC else None,
        scenario_run_id="run-1" if classification is LineageClassification.SYNTHETIC else None,
        stimulus_id="stimulus-1" if classification is LineageClassification.SYNTHETIC else None,
    )
    with pytest.raises(LineageDenied, match=reason):
        require_organic_write(lineage, writer="memory")


def test_explicit_organic_lineage_can_enter_organic_writer():
    lineage = build_event_lineage(
        tenant_id="tenant-a",
        actor_id="actor-a",
        synthetic=False,
    )
    require_organic_write(lineage, writer="fact")


def test_structural_lineage_stamp_rejects_cross_tenant_event():
    lineage = build_event_lineage(
        tenant_id="tenant-a",
        actor_id="actor-synthetic",
        synthetic=True,
        scenario_id="SCN-PE-005",
        scenario_run_id="run-1",
        stimulus_id="stimulus-1",
    )
    event = SimpleNamespace(
        tenant_id="tenant-b",
        lineage_classification="HISTORICAL_UNKNOWN",
        scenario_run_id=None,
        scenario_step_run_id=None,
    )
    with pytest.raises(LineageDenied, match="CROSS_TENANT_EVENT_LINEAGE_DENIED"):
        stamp_structural_lineage(event, lineage, scenario_step_run_id="step-1")


def test_synthetic_binding_rejects_conflicting_metadata():
    binding = SimpleNamespace(
        actor_category="SYNTHETIC_TEST_ACTOR",
        binding_metadata={"synthetic": True, "lineage_classification": "ORGANIC"},
    )
    with pytest.raises(LineageDenied, match="SYNTHETIC_BINDING_METADATA_CONFLICT"):
        require_structurally_synthetic_binding(binding)


def test_stimulus_correlation_is_required_and_unambiguous():
    with pytest.raises(LineageDenied, match="SYNTHETIC_STIMULUS_CORRELATION_MISSING"):
        require_unambiguous_stimulus_id(
            event_stimulus_id=None,
            binding_stimulus_id=None,
        )
    with pytest.raises(LineageDenied, match="SYNTHETIC_STIMULUS_CORRELATION_AMBIGUOUS"):
        require_unambiguous_stimulus_id(
            event_stimulus_id="stimulus-a",
            binding_stimulus_id="stimulus-b",
        )
    assert (
        require_unambiguous_stimulus_id(
            event_stimulus_id="stimulus-a",
            binding_stimulus_id="stimulus-a",
            step_stimulus_ids=("stimulus-a",),
        )
        == "stimulus-a"
    )
