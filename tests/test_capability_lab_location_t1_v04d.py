from sqlalchemy import func, select

from attention_router.application.platform.capability_pack import provision_internal_providers
from attention_router.infrastructure.client_location_models import ClientLocationSnapshotRow
from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.models import (
    CapabilityGrantRow,
    EvidenceReferenceRow,
    ExecutionIntentRow,
    HumanExecutionAuthorizationRow,
    OperationalObservationRow,
    OutboxMessageRow,
    ScenarioRunRow,
)
from attention_router.platform.capability_lab_probe import (
    load_capability_lab_t1_scenarios,
    probe_named_capability_t0,
    record_named_capability_t1_request,
)


T0_SCENARIO = "location.current.no-grant"
T1_SCENARIO = "location.current.pending-human-approval"


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _prepare_location_runtime(session) -> None:
    provision_internal_providers(session, "00000000-0000-4000-8000-000000000001")
    session.commit()
    assert not session.new
    assert not session.dirty
    assert not session.deleted


def test_t1_fixture_is_synthetic_pending_only():
    scenarios = load_capability_lab_t1_scenarios()

    assert set(scenarios) == {T1_SCENARIO}
    scenario = scenarios[T1_SCENARIO]
    assert scenario.capability_key == "location.current"
    assert scenario.t0_scenario_id == T0_SCENARIO
    assert scenario.expected_authorization_state == "PENDING_HUMAN_APPROVAL"
    assert scenario.synthetic_only is True
    assert scenario.production_effects_allowed is False


def test_t1_fails_closed_before_intent_when_t0_prerequisite_is_missing(session):
    before_intents = _count(session, ExecutionIntentRow)
    before_auth = _count(session, HumanExecutionAuthorizationRow)

    try:
        record_named_capability_t1_request(
            session,
            T1_SCENARIO,
            source_sha="source",
            runtime_sha="runtime",
            schema_revision="capability-lab.t1.v1",
            correlation_id="v04d-no-runtime",
        )
    except PermissionError as exc:
        assert str(exc) == "CAPABILITY_LAB_T1_PREREQUISITE_FAILED"
    else:  # pragma: no cover
        raise AssertionError("T1 must fail closed without a passing T0 prerequisite")

    assert _count(session, ExecutionIntentRow) == before_intents
    assert _count(session, HumanExecutionAuthorizationRow) == before_auth


def test_t1_reaches_pending_human_approval_without_grant_or_effect(session):
    _prepare_location_runtime(session)
    protected = {
        HumanIdentityRow: _count(session, HumanIdentityRow),
        CapabilityGrantRow: _count(session, CapabilityGrantRow),
        OutboxMessageRow: _count(session, OutboxMessageRow),
        ScenarioRunRow: _count(session, ScenarioRunRow),
        ClientLocationSnapshotRow: _count(session, ClientLocationSnapshotRow),
    }
    before_intents = _count(session, ExecutionIntentRow)
    before_auth = _count(session, HumanExecutionAuthorizationRow)
    before_observations = _count(session, OperationalObservationRow)
    before_evidence = _count(session, EvidenceReferenceRow)

    report = record_named_capability_t1_request(
        session,
        T1_SCENARIO,
        source_sha="v04d-source",
        runtime_sha="v04d-runtime",
        schema_revision="capability-lab.t1.v1",
        correlation_id="v04d-pending-1",
    )
    session.commit()

    assert report.certification == "DURABLE_T1_PENDING_ONLY"
    assert report.observation.authorization_state == "PENDING_HUMAN_APPROVAL"
    assert report.observation.approval_channel == "meta_whatsapp_interactive"
    assert report.observation.fingerprint_matches is True
    assert report.observation.tenant_scope_matches is True
    assert report.comparison.status == "PASS"

    intent = session.get(ExecutionIntentRow, report.execution_intent_id)
    auth = session.get(HumanExecutionAuthorizationRow, report.authorization_id)
    assert intent is not None and intent.state == "FROZEN"
    assert auth is not None and auth.state == "PENDING_HUMAN_APPROVAL"
    assert auth.execution_intent_id == intent.id
    assert auth.execution_intent_fingerprint == intent.scope_fingerprint
    assert auth.request_wamid is not None and auth.request_wamid.startswith("caplab-t1-")
    assert auth.decision_at is None
    assert auth.decision_inbound_wamid is None
    assert auth.decision_sender is None
    assert auth.decision_button_id is None

    assert _count(session, ExecutionIntentRow) == before_intents + 1
    assert _count(session, HumanExecutionAuthorizationRow) == before_auth + 1
    assert _count(session, OperationalObservationRow) == before_observations + 1
    assert _count(session, EvidenceReferenceRow) == before_evidence + 1
    for model, value in protected.items():
        assert _count(session, model) == value

    t0 = probe_named_capability_t0(session, T0_SCENARIO)
    assert t0.observation.authority_result == "DENY"
    assert t0.observation.reason_code == "CAPABILITY_GRANT_MISSING"


def test_t1_same_correlation_is_rejected_without_second_authorization(session):
    _prepare_location_runtime(session)
    kwargs = dict(
        source_sha="source",
        runtime_sha="runtime",
        schema_revision="capability-lab.t1.v1",
        correlation_id="v04d-correlation-once",
    )
    record_named_capability_t1_request(session, T1_SCENARIO, **kwargs)
    session.commit()

    before_intents = _count(session, ExecutionIntentRow)
    before_auth = _count(session, HumanExecutionAuthorizationRow)

    try:
        record_named_capability_t1_request(session, T1_SCENARIO, **kwargs)
    except PermissionError as exc:
        assert str(exc) == "CAPABILITY_LAB_T1_CORRELATION_REUSED"
    else:  # pragma: no cover
        raise AssertionError("T1 correlation reuse must fail closed")

    assert _count(session, ExecutionIntentRow) == before_intents
    assert _count(session, HumanExecutionAuthorizationRow) == before_auth


def test_t1_requires_explicit_provenance(session):
    _prepare_location_runtime(session)

    try:
        record_named_capability_t1_request(
            session,
            T1_SCENARIO,
            source_sha="",
            runtime_sha="runtime",
            schema_revision="capability-lab.t1.v1",
            correlation_id="v04d-provenance",
        )
    except ValueError as exc:
        assert str(exc) == "CAPABILITY_LAB_T1_PROVENANCE_REQUIRED"
    else:  # pragma: no cover
        raise AssertionError("T1 durable request must require provenance")
