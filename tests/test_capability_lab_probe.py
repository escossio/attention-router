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
from attention_router.platform.api import build_operations_router
from attention_router.platform.capability_lab_probe import (
    capability_lab_scenario,
    load_capability_lab_scenarios,
    probe_capability_t0,
    probe_named_scenario_t0,
)


def _scenarios() -> dict[str, CapabilityLabScenario]:
    return load_capability_lab_scenarios()


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


def test_probe_scenario_loader_is_fixed_to_repository_synthetic_hypotheses():
    scenarios = load_capability_lab_scenarios()

    assert set(scenarios) == {
        "personal.identity.cpf.requires-approval-once",
        "personal.relationship.status.denied",
        "location.current.temporary-grant",
    }
    assert all(item.synthetic_only for item in scenarios.values())
    assert all(not item.uses_real_personal_data for item in scenarios.values())
    assert all(not item.production_effects_allowed for item in scenarios.values())


def test_unknown_probe_scenario_fails_closed():
    try:
        capability_lab_scenario("synthetic.but.not.registered")
    except KeyError as exc:
        assert exc.args == ("CAPABILITY_LAB_SCENARIO_UNKNOWN",)
    else:  # pragma: no cover - contract guard
        raise AssertionError("unknown scenario must fail closed")


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


def test_named_cpf_probe_returns_ephemeral_report_without_mutation(session):
    _sync_registry(session)
    before = _registry_counts(session)

    report = probe_named_scenario_t0(
        session,
        "personal.identity.cpf.requires-approval-once",
    )

    assert _registry_counts(session) == before
    assert not session.new
    assert not session.dirty
    assert not session.deleted
    assert report.certification == "EPHEMERAL_ONLY"
    assert report.probe.durable_evidence is False
    assert report.probe.authority_result == "UNAVAILABLE"
    assert report.probe.reason_code == "UNKNOWN_CAPABILITY"
    assert report.comparison.status == ComparisonStatus.FAIL


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


def test_http_probe_accepts_only_repository_scenario_id():
    router = build_operations_router(
        get_session=lambda: None,
        require_admin=lambda: None,
    )
    path = "/api/v1/admin/platform/operations/capability-lab/probe/{scenario_id}"
    routes = [route for route in router.routes if getattr(route, "path", None) == path]

    assert len(routes) == 1
    route = routes[0]
    assert route.methods == {"GET"}
    assert [item.name for item in route.dependant.path_params] == ["scenario_id"]
    assert [item.name for item in route.dependant.query_params] == []
