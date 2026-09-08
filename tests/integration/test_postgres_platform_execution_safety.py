from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
import uuid

import pytest
from sqlalchemy import func, select

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import (
    AgentDecisionRow,
    AgentExecutionIntentRow,
    ActorBindingRow,
    EffectBudgetRow,
    EffectConsumptionRow,
    ExecutionLeaseRow,
    InboundEventRow,
    InteractionRow,
    ReadinessResultRow,
    ScenarioDefinitionRow,
    ScenarioRunRow,
    ScenarioVersionRow,
)
from attention_router.platform.execution_safety import (
    ClaimReservationRequest,
    EffectDirection,
    ExecutionSafetyService,
    SafetyDenied,
    claim_and_reserve_in_transaction,
    activate_scenario_run_for_execution,
    provision_scenario_execution_safety_in_transaction,
    reserve_synthetic_system_effect_for_intent_in_transaction,
)
from attention_router.platform.scenarios import load_manifest_file, register_manifest_version


pytestmark = pytest.mark.postgres


def _seed(
    Session,
    *,
    lease_count: int,
    stimulus_limit: int,
    lease_expires_delta: timedelta = timedelta(minutes=5),
):
    suffix = uuid.uuid4().hex[:10]
    now = datetime.now(UTC)
    run_id = f"run-{suffix}"
    budget_id = f"budget-{suffix}"
    with Session() as session, session.begin():
        definition = ScenarioDefinitionRow(
            id=f"definition-{suffix}",
            tenant_id=DEFAULT_TENANT_ID,
            scenario_key=f"SCN-TEST-{suffix}",
            title="PostgreSQL execution safety",
            description="concurrency harness",
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
            risk_ids=["RPE-004", "RPE-006"],
            config_keys=["EXECUTION_ONESHOT_LEASE_TTL"],
            risk_classification="P0",
            enabled=True,
            source_sha="test-source",
            created_at=now,
            is_immutable=True,
        )
        run = ScenarioRunRow(
            id=run_id,
            tenant_id=DEFAULT_TENANT_ID,
            scenario_version_id=version.id,
            synthetic_actor_binding_id=None,
            status="RUNNING",
            root_correlation_id=f"correlation-{suffix}",
            source_sha="test-source",
            runtime_sha="test-runtime",
            schema_revision="test-schema",
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
        session.add(definition)
        session.flush()
        session.add(version)
        session.flush()
        session.add(run)
        session.flush()
        budget = EffectBudgetRow(
            id=budget_id,
            tenant_id=DEFAULT_TENANT_ID,
            scenario_run_id=run_id,
            effect_type="WHATSAPP_TEXT",
            target_scope="actor:synthetic-target",
            stimulus_limit=stimulus_limit,
            system_effect_limit=0,
            reserved_count=0,
            consumed_count=0,
            valid_from=now - timedelta(seconds=1),
            valid_until=now + timedelta(minutes=5),
            status="AVAILABLE",
            provenance={"test": "postgres"},
            version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(budget)
        session.flush()
        run.effect_budget_id = budget_id
        for index in range(lease_count):
            session.add(
                ExecutionLeaseRow(
                    id=f"lease-{suffix}-{index}",
                    tenant_id=DEFAULT_TENANT_ID,
                    lease_type="SCENARIO_EFFECT",
                    purpose="postgres concurrency",
                    scope_key=f"scope-{suffix}-{index}",
                    correlation_id=f"correlation-{suffix}",
                    scenario_run_id=run_id,
                    scenario_step_run_id=None,
                    claimant_id=None,
                    status="AVAILABLE",
                    not_before=now - timedelta(minutes=1),
                    expires_at=now + lease_expires_delta,
                    claimed_at=None,
                    consumed_at=None,
                    failed_at=None,
                    cancelled_at=None,
                    claim_count=0,
                    max_claims=1,
                    claimed_event_id=None,
                    logical_execution_id=None,
                    effect_budget_id=budget_id,
                    idempotency_key=f"lease-key-{suffix}-{index}",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
    return suffix, run_id, budget_id


def _request(suffix, run_id, budget_id, *, lease_index, claimant, logical_effect):
    return ClaimReservationRequest(
        tenant_id=DEFAULT_TENANT_ID,
        lease_id=f"lease-{suffix}-{lease_index}",
        budget_id=budget_id,
        claimant_id=claimant,
        claimed_event_id=None,
        logical_execution_id=f"execution-{logical_effect}",
        lease_idempotency_key=f"lease-key-{suffix}-{lease_index}",
        logical_effect_id=logical_effect,
        effect_idempotency_key=f"effect-key-{logical_effect}",
        effect_type="WHATSAPP_TEXT",
        direction=EffectDirection.STIMULUS,
        target_scope="actor:synthetic-target",
        scenario_run_id=run_id,
        provenance={"test": "postgres"},
    )


def _seed_execution_intent(Session, *, suffix: str) -> str:
    now = datetime.now(UTC)
    interaction_id = f"interaction-{suffix}"
    event_id = f"event-{suffix}"
    decision_id = f"decision-{suffix}"
    intent_id = f"intent-{suffix}"
    with Session() as session, session.begin():
        session.add(
            InteractionRow(
                id=interaction_id,
                tenant_id=DEFAULT_TENANT_ID,
                event_type="synthetic_test",
                contact_id=f"actor-ref-{suffix}",
                contact_name="Synthetic fixture",
                relationship_category="synthetic_test",
                active_context=None,
                inbound_text="synthetic fixture",
                state="active",
                policy_id=None,
                policy_version_id=None,
                correlation_id=f"correlation-intent-{suffix}",
                causation_id=None,
                lia_speech=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            InboundEventRow(
                id=event_id,
                tenant_id=DEFAULT_TENANT_ID,
                source="synthetic_fixture",
                external_event_id=f"external-{suffix}",
                event_type="message",
                payload={"fixture": "synthetic"},
                payload_hash=f"hash-{suffix}",
                received_at=now,
                processed_at=now,
                interaction_id=interaction_id,
                status="processed",
                correlation_id=f"correlation-intent-{suffix}",
                lineage_classification="SYNTHETIC",
                scenario_run_id=None,
                scenario_step_run_id=None,
            )
        )
        session.flush()
        session.add(
            AgentDecisionRow(
                id=decision_id,
                event_id=event_id,
                interaction_id=interaction_id,
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
        )
        session.flush()
        session.add(
            AgentExecutionIntentRow(
                id=intent_id,
                agent_decision_id=decision_id,
                response_review_id=None,
                autonomy_evaluation_id=None,
                authorization_source="SCENARIO",
                intent_type="WHATSAPP_RESPONSE",
                effective_response_snapshot="synthetic fixture response",
                status="READY",
                execution_allowed=True,
                external_delivery_allowed=True,
                blocked_reason=None,
                release_status="RELEASED",
                released_at=now,
                released_by="scenario-runner",
                recipient_reference="actor:synthetic-target",
                idempotency_key=f"intent-key-{suffix}",
                capability_name=None,
                capability_request=None,
                provider_instance_id=None,
                canonical_event_id=None,
                created_at=now,
                executed_at=None,
            )
        )
    return intent_id


def test_postgres_two_workers_same_lease_has_one_winner(Session):
    suffix, run_id, budget_id = _seed(Session, lease_count=1, stimulus_limit=1)
    barrier = Barrier(2)

    def attempt(claimant):
        request = _request(
            suffix,
            run_id,
            budget_id,
            lease_index=0,
            claimant=claimant,
            logical_effect=f"effect-{suffix}",
        )
        barrier.wait()
        try:
            return "PASS", ExecutionSafetyService(Session).claim_and_reserve(request)
        except SafetyDenied as exc:
            return "DENY", exc.reason_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ("worker-a", "worker-b")))
    assert [outcome[0] for outcome in outcomes].count("PASS") == 1
    assert [outcome[0] for outcome in outcomes].count("DENY") == 1
    with Session() as session:
        assert session.scalar(
            select(func.count()).select_from(EffectConsumptionRow).where(
                EffectConsumptionRow.effect_budget_id == budget_id,
            )
        ) == 1


def test_postgres_concurrent_budget_reservation_never_exceeds_limit(Session):
    suffix, run_id, budget_id = _seed(Session, lease_count=2, stimulus_limit=1)
    barrier = Barrier(2)

    def attempt(index):
        request = _request(
            suffix,
            run_id,
            budget_id,
            lease_index=index,
            claimant=f"worker-{index}",
            logical_effect=f"effect-{suffix}-{index}",
        )
        barrier.wait()
        try:
            ExecutionSafetyService(Session).claim_and_reserve(request)
            return "PASS"
        except SafetyDenied as exc:
            return exc.reason_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (0, 1)))
    assert outcomes.count("PASS") == 1
    assert outcomes.count("EFFECT_BUDGET_EXHAUSTED") == 1
    with Session() as session:
        budget = session.get(EffectBudgetRow, budget_id)
        assert budget.reserved_count == 1
        count = session.scalar(
            select(func.count()).select_from(EffectConsumptionRow).where(
                EffectConsumptionRow.effect_budget_id == budget_id
            )
        )
        assert count == 1


def test_postgres_fault_before_commit_rolls_back_claim_and_reservation(Session):
    suffix, run_id, budget_id = _seed(Session, lease_count=1, stimulus_limit=1)
    request = _request(
        suffix,
        run_id,
        budget_id,
        lease_index=0,
        claimant="worker-a",
        logical_effect=f"effect-{suffix}",
    )
    with pytest.raises(RuntimeError, match="FI-PE-001"):
        with Session() as session, session.begin():
            claim_and_reserve_in_transaction(session, request)
            raise RuntimeError("FI-PE-001")
    with Session() as session:
        lease = session.get(ExecutionLeaseRow, request.lease_id)
        budget = session.get(EffectBudgetRow, budget_id)
        assert lease.status == "AVAILABLE"
        assert lease.claim_count == 0
        assert budget.reserved_count == 0
        assert session.scalar(
            select(func.count()).select_from(EffectConsumptionRow).where(
                EffectConsumptionRow.effect_budget_id == budget_id
            )
        ) == 0


def test_postgres_restart_reads_durable_claim_and_reservation(Session):
    suffix, run_id, budget_id = _seed(Session, lease_count=1, stimulus_limit=1)
    request = _request(
        suffix,
        run_id,
        budget_id,
        lease_index=0,
        claimant="stable-worker-identity",
        logical_effect=f"effect-{suffix}",
    )
    first = ExecutionSafetyService(Session).claim_and_reserve(request)
    second = ExecutionSafetyService(Session).claim_and_reserve(request)
    assert second.idempotent_retry
    assert not second.reconciliation_required
    assert second.consumption_id == first.consumption_id
    with Session() as session:
        lease = session.get(ExecutionLeaseRow, request.lease_id)
        assert lease.status == "CLAIMED"
        assert lease.claim_count == 1


def test_postgres_synthetic_intent_reservation_is_durable_and_idempotent(Session):
    suffix = uuid.uuid4().hex[:10]
    now = datetime.now(UTC)
    manifest = load_manifest_file(Path("config/platform/scenarios/SCN-PE-011.yaml"))
    with Session() as session, session.begin():
        version = register_manifest_version(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            manifest=manifest,
            manifest_source_path="config/platform/scenarios/SCN-PE-011.yaml",
            source_sha="test-source",
            now=now,
        )
        actor = ActorBindingRow(
            id=f"actor-{suffix}",
            tenant_id=DEFAULT_TENANT_ID,
            source=f"synthetic-driver-{suffix}",
            external_actor_id=f"synthetic-target-{suffix}",
            actor_key=f"synthetic-actor-{suffix}",
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
            id=f"readiness-{suffix}",
            tenant_id=DEFAULT_TENANT_ID,
            dimension="DOMAIN_READINESS",
            subject_type="SCENARIO_RUN",
            subject_key=f"run-{suffix}",
            state="READY",
            reason_codes=[],
            evaluated_at=now,
            evidence_fresh_until=now + timedelta(minutes=2),
            source_references=[],
            blocker_references=[],
            required_dependency_ids=[],
            provenance={"source_sha": "test-source"},
            is_current=True,
            superseded_at=None,
            created_at=now,
        )
        run = ScenarioRunRow(
            id=f"run-{suffix}",
            tenant_id=DEFAULT_TENANT_ID,
            scenario_version_id=version.id,
            synthetic_actor_binding_id=actor.id,
                status="CREATED",
            root_correlation_id=f"correlation-{suffix}",
            source_sha="test-source",
            runtime_sha="test-runtime",
            schema_revision="0019_platform_evolution_wave_d",
            driver_revision="test-driver",
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
        interaction = InteractionRow(
            id=f"interaction-{suffix}",
            tenant_id=DEFAULT_TENANT_ID,
            event_type="message",
            contact_id=actor.actor_key,
            contact_name="Synthetic fixture",
            relationship_category="synthetic_test",
            active_context=None,
            inbound_text="synthetic fixture",
            state="active",
            policy_id=None,
            policy_version_id=None,
            correlation_id=run.root_correlation_id,
            causation_id=None,
            lia_speech=None,
            created_at=now,
            updated_at=now,
        )
        session.add_all([actor, readiness, run, interaction])
        session.flush()
        event = InboundEventRow(
            id=f"event-{suffix}",
            tenant_id=DEFAULT_TENANT_ID,
            source="synthetic_fixture",
            external_event_id=f"external-{suffix}",
            event_type="message",
            payload={
                "scenario_id": manifest.scenario.id,
                "scenario_run_id": run.id,
                "stimulus_id": f"stimulus-{suffix}",
                "actor_id": actor.external_actor_id,
            },
            payload_hash=f"hash-{suffix}",
            received_at=now,
            processed_at=now,
            interaction_id=interaction.id,
            status="processed",
            correlation_id=run.root_correlation_id,
            lineage_classification="SYNTHETIC",
            scenario_run_id=run.id,
            scenario_step_run_id=None,
        )
        session.add(event)
        session.flush()
        decision = AgentDecisionRow(
            id=f"decision-{suffix}",
            event_id=event.id,
            interaction_id=interaction.id,
            agent_blueprint_id=None,
            agent_blueprint_version=None,
            actor_id=actor.actor_key,
            actor_binding_id=actor.id,
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
            reasoning_summary="structured fixture",
            status="READY",
            created_at=now,
        )
        session.add(decision)
        session.flush()
        intent = AgentExecutionIntentRow(
            id=f"intent-{suffix}",
            agent_decision_id=decision.id,
            response_review_id=None,
            autonomy_evaluation_id=None,
            authorization_source="SCENARIO",
            intent_type="WHATSAPP_RESPONSE",
            effective_response_snapshot="synthetic fixture response",
            status="READY",
            execution_allowed=True,
            external_delivery_allowed=True,
            blocked_reason=None,
            release_status="RELEASED",
            released_at=now,
            released_by="scenario-runner",
            recipient_reference=actor.external_actor_id,
            idempotency_key=f"intent-key-{suffix}",
            capability_name=None,
            capability_request=None,
            provider_instance_id=None,
            canonical_event_id=None,
            created_at=now,
            executed_at=None,
        )
        session.add(intent)
        session.flush()
        provision_scenario_execution_safety_in_transaction(
            session,
            scenario_run_id=run.id,
            manifest=manifest,
            readiness_max_age_seconds=60,
            lease_ttl_seconds=120,
            now=now,
        )
        activate_scenario_run_for_execution(
            session,
            scenario_run_id=run.id,
            authority="test-operator",
            requires_bounded_authorization=False,
            now=now,
        )
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
        consumption_id = first.reservation.consumption_id
        lease_id = first.reservation.lease_id

    with Session() as session:
        consumption = session.get(EffectConsumptionRow, consumption_id)
        lease = session.get(ExecutionLeaseRow, lease_id)
        assert consumption.execution_intent_id == f"intent-{suffix}"
        assert consumption.logical_effect_id == f"execution:intent-{suffix}"
        assert lease.logical_execution_id == f"intent-key-{suffix}"
        assert session.scalar(
            select(func.count()).select_from(EffectConsumptionRow).where(
                EffectConsumptionRow.execution_intent_id == f"intent-{suffix}"
            )
        ) == 1


def test_postgres_expired_lease_is_durably_retired(Session):
    suffix, run_id, budget_id = _seed(
        Session,
        lease_count=1,
        stimulus_limit=1,
        lease_expires_delta=timedelta(seconds=-1),
    )
    request = _request(
        suffix,
        run_id,
        budget_id,
        lease_index=0,
        claimant="worker-a",
        logical_effect=f"effect-{suffix}",
    )
    with pytest.raises(SafetyDenied, match="LEASE_EXPIRED"):
        ExecutionSafetyService(Session).claim_and_reserve(request)
    with Session() as session:
        lease = session.get(ExecutionLeaseRow, request.lease_id)
        assert lease.status == "EXPIRED"


def test_postgres_one_execution_intent_cannot_bind_two_consumptions(Session):
    suffix, run_id, budget_id = _seed(Session, lease_count=2, stimulus_limit=2)
    requests = [
        _request(
            suffix,
            run_id,
            budget_id,
            lease_index=index,
            claimant=f"worker-{index}",
            logical_effect=f"effect-{suffix}-{index}",
        )
        for index in (0, 1)
    ]
    reservations = [
        ExecutionSafetyService(Session).claim_and_reserve(request) for request in requests
    ]
    intent_id = _seed_execution_intent(Session, suffix=suffix)
    barrier = Barrier(2)

    def attempt(index):
        barrier.wait()
        try:
            ExecutionSafetyService(Session).bind_reserved_consumption_to_execution_intent(
                tenant_id=DEFAULT_TENANT_ID,
                consumption_id=reservations[index].consumption_id,
                logical_effect_id=requests[index].logical_effect_id,
                execution_intent_id=intent_id,
            )
            return "PASS"
        except SafetyDenied as exc:
            return exc.reason_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (0, 1)))
    assert outcomes.count("PASS") == 1
    assert outcomes.count(
        "EXECUTION_INTENT_ALREADY_LINKED_TO_DIFFERENT_CONSUMPTION"
    ) == 1
    with Session() as session:
        linked_count = session.scalar(
            select(func.count()).select_from(EffectConsumptionRow).where(
                EffectConsumptionRow.execution_intent_id == intent_id
            )
        )
        assert linked_count == 1
