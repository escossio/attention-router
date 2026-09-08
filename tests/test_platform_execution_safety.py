from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from attention_router.application import services
from attention_router.application.execution import TransportProbe
from attention_router.config import settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import (
    AgentDecisionRow,
    AgentExecutionIntentRow,
    AgentResponseReviewRow,
    ActorBindingRow,
    EffectBudgetRow,
    EffectConsumptionRow,
    ExecutionLeaseRow,
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
    ReadinessResultRow,
    ScenarioDefinitionRow,
    ScenarioRunRow,
    ScenarioVersionRow,
    TenantRow,
)
from attention_router.platform.execution_safety import (
    ClaimReservationRequest,
    DispatchSafetyInput,
    EffectDirection,
    ProviderOutcome,
    ReplayDisposition,
    SafetyDenied,
    bind_reserved_consumption_to_execution_intent_in_transaction,
    bind_reserved_consumption_to_outbox_message_in_transaction,
    claim_and_reserve_in_transaction,
    evaluate_dispatch_in_transaction,
    expire_stale_lease_in_transaction,
    finalize_consumed_in_transaction,
    provision_scenario_execution_safety_in_transaction,
    activate_scenario_run_for_execution,
    replay_disposition,
    reserve_synthetic_system_effect_for_intent_in_transaction,
    validate_dispatch_and_bind_outbox_in_transaction,
)
from attention_router.platform.bounded_authorization import create_bounded_authorization
from attention_router.platform.scenarios import (
    ScenarioRunStatus,
    load_manifest_file,
    register_manifest_version,
    transition_scenario_run,
)


def _seed_safety_state(
    session,
    *,
    suffix: str,
    stimulus_limit: int = 1,
    system_limit: int = 0,
    effect_type: str = "WHATSAPP_TEXT",
    lease_status: str = "AVAILABLE",
    expires_delta: timedelta = timedelta(minutes=5),
):
    now = datetime.now(UTC)
    definition = ScenarioDefinitionRow(
        id=f"definition-{suffix}",
        tenant_id=DEFAULT_TENANT_ID,
        scenario_key=f"SCN-PE-{int(suffix):03d}",
        title="safety scenario",
        description="deterministic test contract",
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    version = ScenarioVersionRow(
        id=f"version-{suffix}",
        tenant_id=DEFAULT_TENANT_ID,
        scenario_definition_id=definition.id,
        version=1,
        schema_version="1",
        manifest_source_path="config/platform/scenarios.v1.yaml",
        content_hash=f"hash-{suffix}",
        requirements_covered=["PE-009"],
        risk_ids=["RPE-004"],
        config_keys=["EXECUTION_ONESHOT_LEASE_TTL"],
        risk_classification="P0",
        enabled=True,
        source_sha="source-a",
        created_at=now,
        is_immutable=True,
    )
    run = ScenarioRunRow(
        id=f"run-{suffix}",
        tenant_id=DEFAULT_TENANT_ID,
        scenario_version_id=version.id,
        synthetic_actor_binding_id=None,
        status="RUNNING",
        root_correlation_id=f"correlation-{suffix}",
        source_sha="source-a",
        runtime_sha="runtime-a",
        schema_revision="0019",
        driver_revision=None,
        readiness_result_id=None,
        effect_budget_id=None,
        created_at=now,
        validated_at=now,
        armed_at=now,
        started_at=now,
        verifying_at=None,
        completed_at=None,
        expires_at=now + timedelta(minutes=10),
        terminal_reason=None,
        cleanup_state="PENDING",
        updated_at=now,
    )
    budget = EffectBudgetRow(
        id=f"budget-{suffix}",
        tenant_id=DEFAULT_TENANT_ID,
        scenario_run_id=run.id,
        effect_type=effect_type,
        target_scope="actor:synthetic-target",
        stimulus_limit=stimulus_limit,
        system_effect_limit=system_limit,
        reserved_count=0,
        consumed_count=0,
        valid_from=now - timedelta(seconds=1),
        valid_until=now + timedelta(minutes=5),
        status="AVAILABLE",
        provenance={"source_sha": "source-a"},
        version=1,
        created_at=now,
        updated_at=now,
    )
    lease = ExecutionLeaseRow(
        id=f"lease-{suffix}",
        tenant_id=DEFAULT_TENANT_ID,
        lease_type="SCENARIO_EFFECT",
        purpose="bounded synthetic stimulus",
        scope_key=f"scope-{suffix}",
        correlation_id=f"correlation-{suffix}",
        scenario_run_id=run.id,
        scenario_step_run_id=None,
        claimant_id=None,
        status=lease_status,
        not_before=now - timedelta(minutes=10),
        expires_at=now + expires_delta,
        claimed_at=None,
        consumed_at=None,
        failed_at=None,
        cancelled_at=None,
        claim_count=0,
        max_claims=1,
        claimed_event_id=None,
        logical_execution_id=None,
        effect_budget_id=budget.id,
        idempotency_key=f"lease-key-{suffix}",
        version=1,
        created_at=now,
        updated_at=now,
    )
    run.effect_budget_id = budget.id
    session.add_all([definition, version, run, budget, lease])
    session.flush()
    return now, run, budget, lease


def _request(suffix: str, **overrides):
    values = {
        "tenant_id": DEFAULT_TENANT_ID,
        "lease_id": f"lease-{suffix}",
        "budget_id": f"budget-{suffix}",
        "claimant_id": "worker-a",
        "claimed_event_id": None,
        "logical_execution_id": f"execution-{suffix}",
        "lease_idempotency_key": f"lease-key-{suffix}",
        "logical_effect_id": f"effect-{suffix}",
        "effect_idempotency_key": f"effect-key-{suffix}",
        "effect_type": "WHATSAPP_TEXT",
        "direction": EffectDirection.STIMULUS,
        "target_scope": "actor:synthetic-target",
        "scenario_run_id": f"run-{suffix}",
        "provenance": {"source_sha": "source-a"},
    }
    values.update(overrides)
    return ClaimReservationRequest(**values)


def _seed_synthetic_intent_safety(session, *, suffix: str, provision: bool = True, activate: bool = True):
    now = datetime.now(UTC)
    manifest = load_manifest_file(Path("config/platform/scenarios/SCN-PE-011.yaml"))
    version = register_manifest_version(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        manifest=manifest,
        manifest_source_path="config/platform/scenarios/SCN-PE-011.yaml",
        source_sha="source-a",
        now=now,
    )
    actor = ActorBindingRow(
        id=f"scenario-actor-{suffix}",
        tenant_id=DEFAULT_TENANT_ID,
        source=f"scenario-source-{suffix}",
        external_actor_id=f"scenario-target-{suffix}",
        actor_key=f"actor-ref-{suffix}",
        display_name="Synthetic fixture",
        actor_category="SYNTHETIC_TEST_ACTOR",
        active_context=None,
        is_active=True,
        binding_metadata={
            "synthetic": True,
            "lineage_classification": "SYNTHETIC",
            "stimulus_id": f"stimulus-{suffix}",
        },
        created_at=now,
        updated_at=now,
    )
    readiness = ReadinessResultRow(
        id=f"scenario-readiness-{suffix}",
        tenant_id=DEFAULT_TENANT_ID,
        dimension="DOMAIN_READINESS",
        subject_type="SCENARIO_RUN",
        subject_key=f"scenario-run-{suffix}",
        state="READY",
        reason_codes=[],
        evaluated_at=now,
        evidence_fresh_until=now + timedelta(minutes=2),
        source_references=[],
        blocker_references=[],
        required_dependency_ids=[],
        provenance={"source_sha": "source-a"},
        is_current=True,
        superseded_at=None,
        created_at=now,
    )
    run = ScenarioRunRow(
        id=f"scenario-run-{suffix}",
        tenant_id=DEFAULT_TENANT_ID,
        scenario_version_id=version.id,
        synthetic_actor_binding_id=actor.id,
        status="CREATED",
        root_correlation_id=f"scenario-correlation-{suffix}",
        source_sha="source-a",
        runtime_sha="runtime-a",
        schema_revision="0019_platform_evolution_wave_d",
        driver_revision="driver-a",
        readiness_result_id=readiness.id,
        effect_budget_id=None,
        created_at=now,
        validated_at=None,
        armed_at=None,
        started_at=None,
        verifying_at=None,
        completed_at=None,
        expires_at=now + timedelta(minutes=3),
        terminal_reason=None,
        cleanup_state="PENDING",
        updated_at=now,
    )
    session.add_all([actor, readiness, run])
    session.flush()
    intent, _ = _seed_delivery_chain(session, suffix=suffix)
    decision = session.get(AgentDecisionRow, intent.agent_decision_id)
    event = session.get(InboundEventRow, decision.event_id)
    interaction = session.get(InteractionRow, decision.interaction_id)
    decision.actor_binding_id = actor.id
    event.lineage_classification = "SYNTHETIC"
    event.scenario_run_id = run.id
    event.payload = {
        "scenario_id": manifest.scenario.id,
        "scenario_run_id": run.id,
        "stimulus_id": f"stimulus-{suffix}",
        "actor_id": actor.external_actor_id,
    }
    interaction.contact_id = actor.actor_key
    intent.recipient_reference = actor.external_actor_id
    session.flush()
    provisioning = None
    if provision:
        provisioning = provision_scenario_execution_safety_in_transaction(
            session,
            scenario_run_id=run.id,
            manifest=manifest,
            readiness_max_age_seconds=60,
            lease_ttl_seconds=120,
            now=now,
        )
        if activate:
            activate_scenario_run_for_execution(
                session,
                scenario_run_id=run.id,
                authority="test-operator",
                requires_bounded_authorization=False,
                now=now,
            )
    return now, manifest, run, actor, intent, provisioning


def _seed_delivery_chain(
    session,
    *,
    suffix: str,
    intent_suffix: str = "a",
    intent_type: str = "WHATSAPP_RESPONSE",
    destination: str = "synthetic_transport",
    tenant_id: str = DEFAULT_TENANT_ID,
):
    now = datetime.now(UTC)
    if tenant_id != DEFAULT_TENANT_ID and session.get(TenantRow, tenant_id) is None:
        session.add(
            TenantRow(
                id=tenant_id,
                slug=f"slug-{tenant_id}",
                name="Synthetic fixture tenant",
                status="ACTIVE",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
    interaction = InteractionRow(
        id=f"interaction-{suffix}-{intent_suffix}",
        tenant_id=tenant_id,
        event_type="synthetic_test",
        contact_id=f"actor-ref-{suffix}",
        contact_name="Synthetic fixture",
        relationship_category="synthetic_test",
        active_context=None,
        inbound_text="synthetic fixture",
        state="active",
        policy_id=None,
        policy_version_id=None,
        correlation_id=f"delivery-correlation-{suffix}",
        causation_id=None,
        lia_speech=None,
        created_at=now,
        updated_at=now,
    )
    session.add(interaction)
    session.flush()
    event = InboundEventRow(
        id=f"delivery-event-{suffix}-{intent_suffix}",
        tenant_id=tenant_id,
        source="synthetic_fixture",
        external_event_id=f"fixture-{suffix}-{intent_suffix}",
        event_type="message",
        payload={"fixture": "synthetic"},
        payload_hash=f"fixture-hash-{suffix}-{intent_suffix}",
        received_at=now,
        processed_at=now,
        interaction_id=interaction.id,
        status="processed",
        correlation_id=interaction.correlation_id,
        lineage_classification="SYNTHETIC",
        scenario_run_id=None,
        scenario_step_run_id=None,
    )
    session.add(event)
    session.flush()
    decision = AgentDecisionRow(
        id=f"delivery-decision-{suffix}-{intent_suffix}",
        event_id=event.id,
        interaction_id=interaction.id,
        agent_blueprint_id=None,
        agent_blueprint_version=None,
        actor_id=None,
        actor_binding_id=None,
        audience="synthetic fixture",
        policy_version_id=None,
        decision_pipeline_version="test-v1",
        decision_type="RESPOND",
        recommended_action="reply",
        proposed_response="synthetic fixture response",
        response_message_family=None,
        response_variant_id=None,
        response_spoken_text=None,
        response_introduction_included=None,
        escalation_required=False,
        escalation_reason=None,
        confidence=1,
        missing_information=[],
        intent=None,
        objective=None,
        self_contained=True,
        context_sufficient=True,
        context_requirements=[],
        requested_capabilities=[],
        canonical_event_id=None,
        semantic_source="test",
        response_source="test",
        execution_allowed=True,
        external_delivery_allowed=True,
        reasoning_summary="structured test decision",
        status="READY",
        created_at=now,
    )
    session.add(decision)
    session.flush()
    intent = AgentExecutionIntentRow(
        id=f"delivery-intent-{suffix}-{intent_suffix}",
        agent_decision_id=decision.id,
        response_review_id=None,
        autonomy_evaluation_id=None,
        authorization_source="SCENARIO",
        intent_type=intent_type,
        effective_response_snapshot="synthetic fixture response",
        status="READY",
        execution_allowed=True,
        external_delivery_allowed=True,
        blocked_reason=None,
        release_status="RELEASED",
        released_at=now,
        released_by="scenario-runner",
        recipient_reference="actor:synthetic-target",
        idempotency_key=f"delivery-intent-key-{suffix}-{intent_suffix}",
        capability_name=None,
        capability_request=None,
        provider_instance_id=None,
        canonical_event_id=None,
        created_at=now,
        executed_at=None,
    )
    session.add(intent)
    session.flush()
    outbox = OutboxMessageRow(
        id=f"delivery-outbox-{suffix}-{intent_suffix}",
        interaction_id=interaction.id,
        action_type="dispatch_action",
        destination=destination,
        payload={
            "actor_ref": "actor:synthetic-target",
            "message_type": "text",
            "execution_intent_id": intent.id,
        },
        status="PENDING",
        created_at=now,
        available_at=now,
        claimed_at=None,
        claimed_by=None,
        attempt_count=0,
        last_error=None,
        completed_at=None,
        idempotency_key=f"delivery-outbox-key-{suffix}-{intent_suffix}",
        correlation_id=interaction.correlation_id,
        causation_id=event.id,
        execution_intent_id=intent.id,
    )
    session.add(outbox)
    session.flush()
    return intent, outbox


class _DispatchRecorder:
    def __init__(self):
        self.calls = []

    def dispatch_outbox(self, outbox):
        self.calls.append(outbox.id)
        raise AssertionError("dispatch adapter must not be called after a safety denial")


def _seed_dispatch_ready_platform_outbox(session, *, suffix: str):
    now, run, budget, lease = _seed_safety_state(
        session,
        suffix=suffix,
        stimulus_limit=0,
        system_limit=1,
        effect_type="WHATSAPP_TEXT",
    )
    intent, outbox = _seed_delivery_chain(
        session,
        suffix=suffix,
        destination="local_transport",
    )
    event = session.get(InboundEventRow, f"delivery-event-{suffix}-a")
    event.source = settings.internal_ingress_source
    event.payload = {
        "external_actor_id": "actor:synthetic-target",
        "channel": "whatsapp",
    }
    event.scenario_run_id = run.id
    review = AgentResponseReviewRow(
        id=f"delivery-review-{suffix}",
        agent_decision_id=intent.agent_decision_id,
        status="APPROVED",
        proposed_response_snapshot="synthetic fixture response",
        effective_response="synthetic fixture response",
        reviewer_type="operator",
        reviewer_reference="test-operator",
        review_reason="bounded synthetic scenario",
        version=1,
        created_at=now,
        updated_at=now,
        reviewed_at=now,
    )
    session.add(review)
    intent.response_review_id = review.id
    intent.authorization_source = "HUMAN_REVIEW"
    outbox.action_type = "agent_execution_text"
    outbox.payload = {
        "external_actor_id": "actor:synthetic-target",
        "message_type": "text",
        "text": "synthetic fixture response",
        "execution_intent_id": intent.id,
    }
    readiness = ReadinessResultRow(
        id=f"readiness-{suffix}",
        tenant_id=DEFAULT_TENANT_ID,
        dimension="DOMAIN_READINESS",
        subject_type="SCENARIO_RUN",
        subject_key=run.id,
        state="READY",
        reason_codes=[],
        evaluated_at=now,
        evidence_fresh_until=now + timedelta(minutes=1),
        source_references=[{"kind": "test_fixture"}],
        blocker_references=[],
        required_dependency_ids=[],
        provenance={"source_sha": "source-a"},
        is_current=True,
        superseded_at=None,
        created_at=now,
    )
    session.add(readiness)
    run.readiness_result_id = readiness.id
    session.flush()
    request = _request(
        suffix,
        logical_execution_id=intent.idempotency_key,
        logical_effect_id=outbox.idempotency_key,
        effect_idempotency_key=f"dispatch-effect-key-{suffix}",
        effect_type="WHATSAPP_TEXT",
        direction=EffectDirection.SYSTEM,
    )
    reservation = claim_and_reserve_in_transaction(session, request, now=now)
    bind_reserved_consumption_to_execution_intent_in_transaction(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        consumption_id=reservation.consumption_id,
        logical_effect_id=outbox.idempotency_key,
        execution_intent_id=intent.id,
    )
    bind_reserved_consumption_to_outbox_message_in_transaction(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        consumption_id=reservation.consumption_id,
        logical_effect_id=outbox.idempotency_key,
        execution_intent_id=intent.id,
        outbox_message_id=outbox.id,
    )
    session.commit()
    return run, budget, lease, readiness, outbox


def test_absent_budget_is_zero_permission(session):
    now, *_ = _seed_safety_state(session, suffix="101")
    with pytest.raises(SafetyDenied, match="EFFECT_BUDGET_MISSING"):
        claim_and_reserve_in_transaction(
            session,
            _request("101", budget_id=None),
            now=now,
        )


def test_claim_and_reserve_is_atomic_and_retry_is_same_identity(session):
    now, _, budget, lease = _seed_safety_state(session, suffix="102")
    request = _request("102")
    first = claim_and_reserve_in_transaction(session, request, now=now)
    second = claim_and_reserve_in_transaction(session, request, now=now)
    assert not first.idempotent_retry
    assert not first.reconciliation_required
    assert second.idempotent_retry
    assert not second.reconciliation_required
    assert first.consumption_id == second.consumption_id
    assert lease.status == "CLAIMED"
    assert lease.claim_count == 1
    assert budget.reserved_count == 1


@pytest.mark.parametrize(
    "override,reason",
    [
        ({"tenant_id": "tenant-other"}, "CROSS_TENANT_LEASE_DENIED"),
        ({"target_scope": "actor:other"}, "BUDGET_TARGET_SCOPE_MISMATCH"),
        ({"effect_type": "OTHER_EFFECT"}, "BUDGET_EFFECT_TYPE_MISMATCH"),
        ({"lease_idempotency_key": "different"}, "LEASE_IDEMPOTENCY_KEY_MISMATCH"),
    ],
)
def test_scope_mismatch_fails_closed(session, override, reason):
    now, *_ = _seed_safety_state(session, suffix="103")
    with pytest.raises(SafetyDenied, match=reason):
        claim_and_reserve_in_transaction(session, _request("103", **override), now=now)


def test_zero_budget_and_expired_lease_deny(session):
    now, *_ = _seed_safety_state(session, suffix="104", stimulus_limit=0)
    with pytest.raises(SafetyDenied, match="EFFECT_BUDGET_ZERO"):
        claim_and_reserve_in_transaction(session, _request("104"), now=now)

    now, *_ = _seed_safety_state(
        session,
        suffix="105",
        expires_delta=timedelta(seconds=-1),
    )
    with pytest.raises(SafetyDenied, match="LEASE_EXPIRED"):
        claim_and_reserve_in_transaction(session, _request("105"), now=now)
    assert expire_stale_lease_in_transaction(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        lease_id="lease-105",
        now=now,
    )
    assert session.get(ExecutionLeaseRow, "lease-105").status == "EXPIRED"


def test_expiring_claim_releases_only_provably_undispatched_reservation(session):
    now, _, budget, lease = _seed_safety_state(session, suffix="112")
    reservation = claim_and_reserve_in_transaction(session, _request("112"), now=now)
    assert expire_stale_lease_in_transaction(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        lease_id=lease.id,
        now=now + timedelta(minutes=6),
    )
    consumption = session.get(EffectConsumptionRow, reservation.consumption_id)
    assert lease.status == "EXPIRED"
    assert consumption.state == "RELEASED"
    assert budget.reserved_count == 0
    assert budget.status == "AVAILABLE"


def test_expiring_claim_preserves_outbox_link_for_reconciliation(session):
    now, _, budget, lease = _seed_safety_state(session, suffix="116")
    reservation = claim_and_reserve_in_transaction(session, _request("116"), now=now)
    intent, outbox = _seed_delivery_chain(session, suffix="116")
    bind_reserved_consumption_to_execution_intent_in_transaction(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        consumption_id=reservation.consumption_id,
        logical_effect_id="effect-116",
        execution_intent_id=intent.id,
    )
    bind_reserved_consumption_to_outbox_message_in_transaction(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        consumption_id=reservation.consumption_id,
        logical_effect_id="effect-116",
        execution_intent_id=intent.id,
        outbox_message_id=outbox.id,
    )
    assert expire_stale_lease_in_transaction(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        lease_id=lease.id,
        now=now + timedelta(minutes=6),
    )
    consumption = session.get(EffectConsumptionRow, reservation.consumption_id)
    assert lease.status == "EXPIRED"
    assert consumption.state == "RESERVED"
    assert consumption.outbox_message_id == outbox.id
    assert budget.reserved_count == 1


def test_finalize_consumes_lease_and_budget_without_new_identity(session):
    now, _, budget, lease = _seed_safety_state(session, suffix="106")
    reservation = claim_and_reserve_in_transaction(session, _request("106"), now=now)
    finalize_consumed_in_transaction(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        lease_id=lease.id,
        consumption_id=reservation.consumption_id,
        logical_execution_id="execution-106",
        logical_effect_id="effect-106",
        now=now,
    )
    finalize_consumed_in_transaction(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        lease_id=lease.id,
        consumption_id=reservation.consumption_id,
        logical_execution_id="execution-106",
        logical_effect_id="effect-106",
        now=now,
    )
    assert lease.status == "CONSUMED"
    assert budget.reserved_count == 0
    assert budget.consumed_count == 1


def test_predispatch_rechecks_gate_and_terminal_scenario(session):
    now, run, _, _ = _seed_safety_state(session, suffix="107")
    reservation = claim_and_reserve_in_transaction(session, _request("107"), now=now)
    request = DispatchSafetyInput(
        tenant_id=DEFAULT_TENANT_ID,
        lease_id="lease-107",
        budget_id="budget-107",
        consumption_id=reservation.consumption_id,
        logical_execution_id="execution-107",
        logical_effect_id="effect-107",
        scenario_run_id="run-107",
        effect_type="WHATSAPP_TEXT",
        direction=EffectDirection.STIMULUS,
        target_scope="actor:synthetic-target",
        global_gate_open=False,
        policy_allowed=True,
        readiness_state="READY",
        transport_ready=True,
    )
    assert evaluate_dispatch_in_transaction(session, request, now=now).reason_code == (
        "GLOBAL_EXTERNAL_GATE_CLOSED"
    )

    transition_scenario_run(run, ScenarioRunStatus.FAILED, reason="test failure", now=now)
    allowed_gates = replace(request, global_gate_open=True)
    decision = evaluate_dispatch_in_transaction(session, allowed_gates, now=now)
    assert not decision.allowed
    assert decision.reason_code == "TERMINAL_SCENARIO_FAILED_DENIES"


@pytest.mark.parametrize("terminal_budget_status", ["CANCELLED", "RELEASED", "CONSUMED"])
def test_predispatch_denies_terminal_budget_and_expired_scenario(
    session,
    terminal_budget_status,
):
    now, run, budget, _ = _seed_safety_state(session, suffix="108")
    reservation = claim_and_reserve_in_transaction(session, _request("108"), now=now)
    request = DispatchSafetyInput(
        tenant_id=DEFAULT_TENANT_ID,
        lease_id="lease-108",
        budget_id="budget-108",
        consumption_id=reservation.consumption_id,
        logical_execution_id="execution-108",
        logical_effect_id="effect-108",
        scenario_run_id="run-108",
        effect_type="WHATSAPP_TEXT",
        direction=EffectDirection.STIMULUS,
        target_scope="actor:synthetic-target",
        global_gate_open=True,
        policy_allowed=True,
        readiness_state="READY",
        transport_ready=True,
    )
    budget.status = terminal_budget_status
    assert evaluate_dispatch_in_transaction(session, request, now=now).reason_code == (
        f"BUDGET_STATUS_{terminal_budget_status}_DENIES"
    )
    budget.status = "RESERVED"
    run.expires_at = now
    assert evaluate_dispatch_in_transaction(session, request, now=now).reason_code == (
        "SCENARIO_RUN_EXPIRED"
    )


def test_reserved_consumption_links_are_exact_immutable_and_idempotent(session):
    now, *_ = _seed_safety_state(session, suffix="109")
    reservation = claim_and_reserve_in_transaction(session, _request("109"), now=now)
    intent, outbox = _seed_delivery_chain(session, suffix="109")

    first = bind_reserved_consumption_to_execution_intent_in_transaction(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        consumption_id=reservation.consumption_id,
        logical_effect_id="effect-109",
        execution_intent_id=intent.id,
    )
    second = bind_reserved_consumption_to_execution_intent_in_transaction(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        consumption_id=reservation.consumption_id,
        logical_effect_id="effect-109",
        execution_intent_id=intent.id,
    )
    assert not first.idempotent
    assert second.idempotent

    first_outbox = bind_reserved_consumption_to_outbox_message_in_transaction(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        consumption_id=reservation.consumption_id,
        logical_effect_id="effect-109",
        execution_intent_id=intent.id,
        outbox_message_id=outbox.id,
    )
    second_outbox = bind_reserved_consumption_to_outbox_message_in_transaction(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        consumption_id=reservation.consumption_id,
        logical_effect_id="effect-109",
        execution_intent_id=intent.id,
        outbox_message_id=outbox.id,
    )
    assert not first_outbox.idempotent
    assert second_outbox.idempotent
    row = session.get(EffectConsumptionRow, reservation.consumption_id)
    assert row.execution_intent_id == intent.id
    assert row.outbox_message_id == outbox.id

    other_intent, _ = _seed_delivery_chain(session, suffix="109", intent_suffix="b")
    with pytest.raises(
        SafetyDenied,
        match="CONSUMPTION_EXECUTION_INTENT_LINK_IMMUTABLE",
    ):
        bind_reserved_consumption_to_execution_intent_in_transaction(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            consumption_id=reservation.consumption_id,
            logical_effect_id="effect-109",
            execution_intent_id=other_intent.id,
        )


def test_consumption_link_rejects_intent_and_outbox_scope_mismatch(session):
    now, *_ = _seed_safety_state(session, suffix="113")
    reservation = claim_and_reserve_in_transaction(session, _request("113"), now=now)
    intent, outbox = _seed_delivery_chain(session, suffix="113")
    intent.recipient_reference = "actor:different-target"
    with pytest.raises(SafetyDenied, match="EXECUTION_INTENT_TARGET_SCOPE_MISMATCH"):
        bind_reserved_consumption_to_execution_intent_in_transaction(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            consumption_id=reservation.consumption_id,
            logical_effect_id="effect-113",
            execution_intent_id=intent.id,
        )
    intent.recipient_reference = "actor:synthetic-target"
    bind_reserved_consumption_to_execution_intent_in_transaction(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        consumption_id=reservation.consumption_id,
        logical_effect_id="effect-113",
        execution_intent_id=intent.id,
    )
    outbox.payload = {**outbox.payload, "actor_ref": "actor:different-target"}
    with pytest.raises(SafetyDenied, match="OUTBOX_PAYLOAD_TARGET_SCOPE_MISMATCH"):
        bind_reserved_consumption_to_outbox_message_in_transaction(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            consumption_id=reservation.consumption_id,
            logical_effect_id="effect-113",
            execution_intent_id=intent.id,
            outbox_message_id=outbox.id,
        )


def test_system_effect_finalization_requires_exact_delivery_links(session):
    now, *_ = _seed_safety_state(
        session,
        suffix="114",
        stimulus_limit=0,
        system_limit=1,
        effect_type="WHATSAPP_RESPONSE",
    )
    request = _request(
        "114",
        direction=EffectDirection.SYSTEM,
        effect_type="WHATSAPP_RESPONSE",
    )
    reservation = claim_and_reserve_in_transaction(session, request, now=now)
    with pytest.raises(
        SafetyDenied,
        match="FINALIZATION_SYSTEM_EFFECT_DELIVERY_LINK_MISSING",
    ):
        finalize_consumed_in_transaction(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            lease_id=request.lease_id,
            consumption_id=reservation.consumption_id,
            logical_execution_id=request.logical_execution_id,
            logical_effect_id=request.logical_effect_id,
            now=now,
        )

    intent, outbox = _seed_delivery_chain(
        session,
        suffix="114",
        destination="local_transport",
    )
    bind_reserved_consumption_to_execution_intent_in_transaction(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        consumption_id=reservation.consumption_id,
        logical_effect_id=request.logical_effect_id,
        execution_intent_id=intent.id,
    )
    bind_reserved_consumption_to_outbox_message_in_transaction(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        consumption_id=reservation.consumption_id,
        logical_effect_id=request.logical_effect_id,
        execution_intent_id=intent.id,
        outbox_message_id=outbox.id,
    )
    finalize_consumed_in_transaction(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        lease_id=request.lease_id,
        consumption_id=reservation.consumption_id,
        logical_execution_id=request.logical_execution_id,
        logical_effect_id=request.logical_effect_id,
        now=now,
    )


def test_finalization_rejects_budget_counter_corruption(session):
    now, _, budget, lease = _seed_safety_state(session, suffix="115")
    reservation = claim_and_reserve_in_transaction(session, _request("115"), now=now)
    budget.reserved_count = 0
    with pytest.raises(
        SafetyDenied,
        match="FINALIZATION_BUDGET_RESERVED_COUNT_INCONSISTENT",
    ):
        finalize_consumed_in_transaction(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            lease_id=lease.id,
            consumption_id=reservation.consumption_id,
            logical_execution_id="execution-115",
            logical_effect_id="effect-115",
            now=now,
        )


def test_dispatch_validation_and_outbox_link_share_transaction(session):
    now, *_ = _seed_safety_state(session, suffix="110")
    reservation = claim_and_reserve_in_transaction(session, _request("110"), now=now)
    intent, outbox = _seed_delivery_chain(session, suffix="110")
    bind_reserved_consumption_to_execution_intent_in_transaction(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        consumption_id=reservation.consumption_id,
        logical_effect_id="effect-110",
        execution_intent_id=intent.id,
    )
    request = DispatchSafetyInput(
        tenant_id=DEFAULT_TENANT_ID,
        lease_id="lease-110",
        budget_id="budget-110",
        consumption_id=reservation.consumption_id,
        logical_execution_id="execution-110",
        logical_effect_id="effect-110",
        scenario_run_id="run-110",
        effect_type="WHATSAPP_TEXT",
        direction=EffectDirection.STIMULUS,
        target_scope="actor:synthetic-target",
        global_gate_open=True,
        policy_allowed=True,
        readiness_state="READY",
        transport_ready=True,
    )
    result = validate_dispatch_and_bind_outbox_in_transaction(
        session,
        request,
        execution_intent_id=intent.id,
        outbox_message_id=outbox.id,
        now=now,
    )
    assert result.outbox_message_id == outbox.id


def test_consumption_link_derives_and_enforces_tenant(session):
    now, *_ = _seed_safety_state(session, suffix="111")
    reservation = claim_and_reserve_in_transaction(session, _request("111"), now=now)
    intent, _ = _seed_delivery_chain(
        session,
        suffix="111",
        tenant_id="tenant-other",
    )
    with pytest.raises(SafetyDenied, match="CROSS_TENANT_EXECUTION_INTENT_DENIED"):
        bind_reserved_consumption_to_execution_intent_in_transaction(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            consumption_id=reservation.consumption_id,
            logical_effect_id="effect-111",
            execution_intent_id=intent.id,
        )


def test_ambiguous_provider_outcome_never_blindly_retries():
    assert replay_disposition(ProviderOutcome.AMBIGUOUS) is (
        ReplayDisposition.RECONCILIATION_REQUIRED
    )
    assert replay_disposition(ProviderOutcome.SENT) is ReplayDisposition.DO_NOT_RETRY
    assert replay_disposition(ProviderOutcome.PROVED_NOT_SENT) is (
        ReplayDisposition.RETRY_SAME_LOGICAL_EFFECT
    )
    with pytest.raises(ValueError, match="EXTERNAL_EFFECT_BLIND_RETRIES_MUST_EQUAL_ZERO"):
        replay_disposition(ProviderOutcome.AMBIGUOUS, external_effect_blind_retries=1)


def test_synthetic_scenario_provisions_and_binds_exact_intent_once(session):
    now, manifest, run, _, intent, provisioning = _seed_synthetic_intent_safety(
        session,
        suffix="301",
    )
    assert run.status == "ARMED"
    assert provisioning is not None
    same_provisioning = provision_scenario_execution_safety_in_transaction(
        session,
        scenario_run_id=run.id,
        manifest=manifest,
        readiness_max_age_seconds=60,
        lease_ttl_seconds=120,
        now=now,
    )
    assert same_provisioning.idempotent
    first = reserve_synthetic_system_effect_for_intent_in_transaction(
        session,
        intent=intent,
        readiness_max_age_seconds=60,
        now=now,
    )
    second = reserve_synthetic_system_effect_for_intent_in_transaction(
        session,
        intent=intent,
        readiness_max_age_seconds=60,
        now=now,
    )
    assert first is not None and second is not None
    assert first.reservation.consumption_id == second.reservation.consumption_id
    assert second.reservation.idempotent_retry
    consumption = session.get(EffectConsumptionRow, first.reservation.consumption_id)
    lease = session.get(ExecutionLeaseRow, first.reservation.lease_id)
    assert consumption.execution_intent_id == intent.id
    assert consumption.logical_effect_id == f"execution:{intent.id}"
    assert lease.logical_execution_id == intent.idempotency_key
    assert session.scalar(select(func.count()).select_from(EffectConsumptionRow)) == 1


def test_scenario_safety_provisioning_is_inert_until_explicit_activation(session):
    _, _, run, _, _, provisioning = _seed_synthetic_intent_safety(
        session, suffix="302", activate=False
    )
    assert provisioning is not None
    assert run.status == "CREATED"
    lease = session.get(ExecutionLeaseRow, provisioning.system_lease_id)
    assert lease is not None
    assert lease.status == "AVAILABLE"
    assert lease.claim_count == 0
    with pytest.raises(SafetyDenied, match="AUTHORIZATION_MISSING"):
        activate_scenario_run_for_execution(
            session,
            scenario_run_id=run.id,
            authority="test-operator",
            now=datetime.now(UTC),
        )
    assert run.status == "CREATED"


def test_explicit_activation_arms_prepared_run_without_claiming_effect(session):
    now, _, run, actor, intent, provisioning = _seed_synthetic_intent_safety(
        session, suffix="306", activate=False
    )
    assert provisioning is not None
    create_bounded_authorization(
        session,
        tenant_id=run.tenant_id,
        scenario_run_id=run.id,
        effect_budget_id=provisioning.system_budget_id,
        level="L1",
        actor_scope=actor.actor_key,
        target_scope=actor.external_actor_id,
        capability_scope="conversation.reply",
        effect_scope=f"execution:{intent.id}",
        max_effects=1,
        authorized_by="test-operator",
        correlation_id=run.root_correlation_id,
        valid_from=now,
        expires_at=now + timedelta(minutes=5),
        now=now,
    )
    create_bounded_authorization(
        session,
        tenant_id=run.tenant_id,
        scenario_run_id=run.id,
        effect_budget_id=provisioning.stimulus_budget_id,
        level="L1",
        actor_scope=actor.actor_key,
        target_scope=actor.external_actor_id,
        capability_scope="synthetic_send_bounded",
        effect_scope="WHATSAPP_STIMULUS",
        max_effects=1,
        authorized_by="test-operator",
        correlation_id=run.root_correlation_id,
        valid_from=now,
        expires_at=now + timedelta(minutes=5),
        now=now,
    )
    assert activate_scenario_run_for_execution(
        session,
        scenario_run_id=run.id,
        authority="test-operator",
        level="L1",
        actor_scope=actor.actor_key,
        target_scope=actor.external_actor_id,
        capability_scope="conversation.reply",
        effect_scope=f"execution:{intent.id}",
        now=now,
    )
    assert run.status == "ARMED"
    lease = session.get(ExecutionLeaseRow, provisioning.system_lease_id)
    assert lease is not None and lease.status == "AVAILABLE" and lease.claim_count == 0
    assert session.scalar(select(func.count()).select_from(EffectConsumptionRow)) == 0
    assert not activate_scenario_run_for_execution(
        session,
        scenario_run_id=run.id,
        authority="test-operator",
        now=now,
    )


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("absent", "EFFECT_BUDGET_MISSING"),
        ("zero", "EFFECT_BUDGET_ZERO"),
        ("expired", "EFFECT_BUDGET_EXPIRED"),
        ("cross_tenant", "CROSS_TENANT_SYNTHETIC_SCENARIO_DENIED"),
    ],
)
def test_synthetic_intent_reservation_denies_missing_or_invalid_scope(
    session,
    mode,
    reason,
):
    now, _, run, _, intent, provisioning = _seed_synthetic_intent_safety(
        session,
        suffix={
            "absent": "302",
            "zero": "303",
            "expired": "304",
            "cross_tenant": "305",
        }[mode],
        provision=mode != "absent",
    )
    evaluation_now = now
    if mode == "absent":
        run.status = "ARMED"
        run.armed_at = now
    if mode in {"zero", "expired"}:
        budget = session.get(EffectBudgetRow, provisioning.system_budget_id)
        if mode == "zero":
            budget.system_effect_limit = 0
        else:
            budget.valid_until = now + timedelta(seconds=1)
            evaluation_now = now + timedelta(seconds=2)
    elif mode == "cross_tenant":
        other_tenant = TenantRow(
            id="tenant-synthetic-other",
            slug="tenant-synthetic-other",
            name="Synthetic other tenant",
            status="ACTIVE",
            created_at=now,
            updated_at=now,
        )
        session.add(other_tenant)
        session.flush()
        decision = session.get(AgentDecisionRow, intent.agent_decision_id)
        event = session.get(InboundEventRow, decision.event_id)
        interaction = session.get(InteractionRow, decision.interaction_id)
        event.tenant_id = other_tenant.id
        interaction.tenant_id = other_tenant.id
        session.flush()
    with pytest.raises(SafetyDenied, match=reason):
        reserve_synthetic_system_effect_for_intent_in_transaction(
            session,
            intent=intent,
            readiness_max_age_seconds=60,
            now=evaluation_now,
        )
    assert session.scalar(select(func.count()).select_from(EffectConsumptionRow)) == 0


def test_organic_intent_does_not_reserve_platform_safety_state(session):
    intent, _ = _seed_delivery_chain(session, suffix="306")
    event = session.get(
        InboundEventRow,
        session.get(AgentDecisionRow, intent.agent_decision_id).event_id,
    )
    event.lineage_classification = "ORGANIC"
    event.scenario_run_id = None
    event.payload = {"fixture": "organic"}
    assert (
        reserve_synthetic_system_effect_for_intent_in_transaction(
            session,
            intent=intent,
            readiness_max_age_seconds=60,
        )
        is None
    )
    assert session.scalar(select(func.count()).select_from(EffectConsumptionRow)) == 0


def test_agent_execution_outbox_without_exact_intent_is_blocked_before_network(
    session,
    monkeypatch,
):
    _, outbox = _seed_delivery_chain(session, suffix="307")
    outbox.action_type = "agent_execution_text"
    outbox.destination = "local_transport"
    outbox.payload = {"fixture": "missing-intent"}
    outbox.execution_intent_id = None
    session.commit()
    adapter = _DispatchRecorder()
    monkeypatch.setattr(services, "local_transport_outbound", adapter)
    assert services.process_outbox(session, "worker-missing-intent") == 1
    session.refresh(outbox)
    assert outbox.status == "BLOCKED"
    assert outbox.last_error == "AGENT_EXECUTION_INTENT_MISSING"
    assert adapter.calls == []


@pytest.mark.parametrize(
    ("denied_layer", "suffix", "expected_reason"),
    [
        (
            "transport_external_gate",
            "201",
            "TRANSPORT_EXTERNAL_DELIVERY_DISABLED",
        ),
        ("readiness", "202", "READINESS_STALE_DENIES"),
        ("lease", "203", "LEASE_EXPIRED"),
        ("budget", "204", "BUDGET_STATUS_CONSUMED_DENIES"),
        ("scenario", "205", "TERMINAL_SCENARIO_FAILED_DENIES"),
    ],
)
def test_platform_outbox_revalidates_safety_immediately_before_adapter(
    session,
    monkeypatch,
    denied_layer,
    suffix,
    expected_reason,
):
    run, budget, lease, readiness, outbox = _seed_dispatch_ready_platform_outbox(
        session,
        suffix=suffix,
    )
    adapter = _DispatchRecorder()
    monkeypatch.setattr(settings, "agent_execution_enabled", True)
    monkeypatch.setattr(settings, "external_delivery_enabled", True)
    monkeypatch.setattr(services, "local_transport_outbound", adapter)

    def mutate_state_after_claim_and_probe_transport():
        if denied_layer == "readiness":
            readiness.evidence_fresh_until = datetime.now(UTC) - timedelta(seconds=1)
        elif denied_layer == "lease":
            lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif denied_layer == "budget":
            budget.status = "CONSUMED"
        elif denied_layer == "scenario":
            run.status = "FAILED"
            run.completed_at = datetime.now(UTC)
            run.terminal_reason = "simulated pre-dispatch terminal transition"
        session.flush()
        return TransportProbe(
            ready=True,
            external_delivery_enabled=denied_layer != "transport_external_gate",
        )

    monkeypatch.setattr(
        services,
        "probe_transport_status",
        mutate_state_after_claim_and_probe_transport,
    )

    assert services.process_outbox(session, f"worker-{suffix}") == 1

    session.refresh(outbox)
    assert outbox.status == "BLOCKED"
    assert outbox.last_error == expected_reason
    assert outbox.attempt_count == 1
    assert adapter.calls == []
