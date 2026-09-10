import json
from pathlib import Path

from sqlalchemy import func, select

from attention_router.application.platform.registry import sync_platform_registry
from attention_router.core.capability_lab import (
    CapabilityLabScenario,
    ComparisonStatus,
    compare_scenario,
)
from attention_router.infrastructure.models import (
    CapabilityDefinitionRow,
    CapabilityGrantRow,
    CapabilityVersionRow,
    ProviderBindingRow,
    ProviderDefinitionRow,
    ProviderInstanceRow,
)
from attention_router.platform.capability_lab_probe import probe_capability_t0


FIXTURE = Path("attention_router/web/static/capability-lab-scenarios.json")


def _scenarios() -> dict[str, CapabilityLabScenario]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    scenarios = [CapabilityLabScenario.model_validate(item) for item in payload]
    return {item.scenario_id: item for item in scenarios}


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _registry_counts(session) -> dict[type, int]:
    models = (
        CapabilityDefinitionRow,
        CapabilityVersionRow,
        CapabilityGrantRow,
        ProviderDefinitionRow,
        ProviderInstanceRow,
        ProviderBindingRow,
    )
    return {model: _count(session, model) for model in models}


def _sync_registry(session) -> None:
    sync_platform_registry(session)
    session.commit()
    assert not session.new
    assert not session.dirty
    assert not session.deleted


def test_unknown_cpf_probe_reports_canonical_unknown_without_mutation(session):
    _sync_registry(session)
    scenario = _scenarios()["personal.identity.cpf.requires-approval-once"]
    before = _registry_counts(session)

    result = probe_capability_t0(session, scenario)

    assert _registry_counts(session) == before
    assert not session.new
    assert not session.dirty
    assert not session.deleted
    assert result.capability_key == "personal.identity.cpf"
    assert result.resolution_status == "UNKNOWN"
    assert result.authority_result == "UNAVAILABLE"
    assert result.reason_code == "UNKNOWN_CAPABILITY"
    assert result.durable_evidence is False
    assert result.observation.resolution_evidence_refs == []

    comparison = compare_scenario(scenario, result.observation)
    assert comparison.status == ComparisonStatus.FAIL
    assert comparison.mismatches == [
        "RESOLUTION_MISMATCH:expected=REQUIRES_APPROVAL;observed=UNAVAILABLE"
    ]


def test_registered_location_probe_reports_provider_unavailable_without_mutation(session):
    _sync_registry(session)
    scenario = _scenarios()["location.current.temporary-grant"]
    before = _registry_counts(session)

    result = probe_capability_t0(session, scenario)

    assert _registry_counts(session) == before
    assert result.resolution_status == "KNOWN_BUT_UNAVAILABLE"
    assert result.authority_result == "UNAVAILABLE"
    assert result.reason_code == "CAPABILITY_UNAVAILABLE"
    assert result.provider_interface == "LocationProvider"
    assert result.approval_required is False
    assert result.execution_allowed is False


def test_operational_control_proves_current_no_grant_denial_semantics(session):
    _sync_registry(session)
    control = CapabilityLabScenario(
        scenario_id="callback.request.no-grant-control",
        title="Operational no-grant approval control",
        description=(
            "Use an existing operational capability to observe canonical no-grant authority."
        ),
        capability_key="callback.request",
        requester_actor_key="synthetic:contact.control",
        request_text="Synthetic callback request",
        expected_resolution="REQUIRES_APPROVAL",
    )
    before = _registry_counts(session)

    result = probe_capability_t0(session, control)

    assert _registry_counts(session) == before
    assert result.resolution_status == "AVAILABLE_NOT_AUTHORIZED"
    assert result.authority_result == "DENY"
    assert result.reason_code == "CAPABILITY_GRANT_MISSING"
    assert result.provider_interface is None
    assert result.approval_required is False
    assert result.execution_allowed is False

    comparison = compare_scenario(control, result.observation)
    assert comparison.status == ComparisonStatus.FAIL
    assert comparison.mismatches == [
        "RESOLUTION_MISMATCH:expected=REQUIRES_APPROVAL;observed=DENY"
    ]


def test_matching_ephemeral_probe_stays_incomplete_without_durable_t0_evidence(session):
    _sync_registry(session)
    hypothesis = CapabilityLabScenario(
        scenario_id="callback.request.current-behavior-control",
        title="Current callback authority behavior",
        description="Match current runtime behavior but require evidence before certification.",
        capability_key="callback.request",
        requester_actor_key="synthetic:contact.control",
        request_text="Synthetic callback request",
        expected_resolution="DENY",
    )

    result = probe_capability_t0(session, hypothesis)
    comparison = compare_scenario(hypothesis, result.observation)

    assert result.authority_result == "DENY"
    assert result.reason_code == "CAPABILITY_GRANT_MISSING"
    assert comparison.status == ComparisonStatus.INCOMPLETE
    assert comparison.mismatches == []
    assert comparison.incomplete_reasons == ["T0_RESOLUTION_EVIDENCE_MISSING"]
    assert comparison.evidence_refs == []
