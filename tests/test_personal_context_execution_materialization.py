from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from attention_router.application.personal_context_execution_authority import (
    evaluate_accepted_recommendation_authority,
)
from attention_router.application.personal_context_execution_materialization import (
    RecommendationMaterializationError,
    materialize_prepared_recommendation_execution,
)
from attention_router.application.personal_context_recommendation_lifecycle import (
    persist_context_recommendation,
    resolve_context_recommendation,
)
from attention_router.application.personal_context_recommendations import (
    ContextRecommendation,
)
from attention_router.application.platform.authority import (
    create_capability_grant,
    revoke_capability_grant,
)
from attention_router.application.platform.capability_pack import (
    provision_internal_providers,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    AgentExecutionIntentRow,
    ExecutionIntentRow,
    FactRow,
    InboundEventRow,
    MemoryActorRow,
    MemoryClaimRow,
    OutboxMessageRow,
    ProviderInstanceRow,
    ReminderRow,
    TenantRow,
)
from attention_router.infrastructure.repository import (
    create_policy,
    deactivate_policy,
    upsert_actor_binding,
)


ACTOR = "owner-v1f"
EXTERNAL_ACTOR = "owner-v1f-external"
RECOMMENDATION_ID = "recommendation-v1f"
POLICY_ID = "v1f-owner-policy"


def _ensure_tenant(session) -> None:
    if session.get(TenantRow, DEFAULT_TENANT_ID) is not None:
        return
    stamp = now_utc()
    session.add(
        TenantRow(
            id=DEFAULT_TENANT_ID,
            slug=DEFAULT_TENANT_ID,
            name="default",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()


def _owner(session):
    _ensure_tenant(session)
    binding = upsert_actor_binding(
        session,
        "wwebjs",
        EXTERNAL_ACTOR,
        ACTOR,
        "owner",
        metadata={"owner": True, "audience": "owner"},
        tenant_id=DEFAULT_TENANT_ID,
    )
    actor = MemoryActorRow(
        id=new_id(),
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        metadata_json={},
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(actor)
    session.flush()
    return binding, actor


def _pattern(session, actor: MemoryActorRow, stamp: datetime) -> MemoryClaimRow:
    row = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=actor.id,
        subject_entity_id=None,
        predicate="context.pattern.temporal_recurrence",
        object_type="JSON",
        object_text=None,
        object_actor_id=None,
        object_entity_id=None,
        object_json={
            "pattern_type": "TEMPORAL_RECURRENCE",
            "event_type": "LOCATION_ARRIVAL",
            "signature_kind": "PATTERN_KEY",
            "signature_value": "gym-arrival",
            "cadence_seconds": 86400,
            "evidence_class": "INFERRED",
            "hypothesis_status": "HYPOTHESIS",
            "grants_authority": False,
            "recommendation_ready": False,
        },
        context={
            "hypothesis_id": "pattern-v1f",
            "snapshot_fingerprint": "sha256:v1f",
            "evidence_timeline_event_ids": ["evt-1", "evt-2", "evt-3"],
            "source_provenance": ["android-location"],
            "occurrence_count": 3,
            "anomaly_count": 0,
            "support_ratio": 1.0,
        },
        confidence=0.85,
        sensitivity_class="PRIVATE",
        source_quality="DERIVED_PATTERN",
        valid_from=stamp - timedelta(days=3),
        valid_until=stamp + timedelta(days=3),
        status="ACTIVE",
        staleness_class="PERISHABLE",
        supersedes_claim_id=None,
        conflict_group_id=None,
        first_observed_at=stamp - timedelta(days=2),
        last_observed_at=stamp,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row


def _recommendation(source: MemoryClaimRow, stamp: datetime) -> ContextRecommendation:
    return ContextRecommendation(
        recommendation_id=RECOMMENDATION_ID,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        source_claim_id=source.id,
        recommendation_type="REMINDER_FOR_RECURRENT_ARRIVAL",
        status="PROPOSED",
        explanation="Quer que eu prepare um lembrete para a próxima ocorrência prevista?",
        capability_name="reminder.create",
        capability_availability="OPERATIONAL",
        suggested_parameters={
            "summary": "Recorrência observada pela Andy",
            "trigger_at": (stamp + timedelta(days=1)).isoformat(),
        },
        confidence=0.85,
        support_ratio=1.0,
        occurrence_count=3,
        anomaly_count=0,
        source_provenance=("android-location",),
        generated_at=stamp,
        valid_until=stamp + timedelta(days=1),
        requires_user_confirmation=True,
        execution_requested=False,
        grants_authority=False,
    )


def _accept_event(session, stamp: datetime) -> InboundEventRow:
    payload = {
        "actor_id": EXTERNAL_ACTOR,
        "content": "sim",
        "event_origin": "OWNER_COMMAND",
        "owner_authenticated": True,
        "metadata": {
            "from_me": True,
            "owner_self_chat": True,
            "from_me_classification": "OWNER_COMMAND",
            "final_from_me_classification": "OWNER_COMMAND",
        },
    }
    row = InboundEventRow(
        id=new_id(),
        tenant_id=DEFAULT_TENANT_ID,
        source="wwebjs",
        external_event_id="v1f-accept",
        event_type="message",
        payload=payload,
        payload_hash=stable_hash(payload),
        received_at=stamp,
        processed_at=None,
        interaction_id=None,
        status="RECEIVED",
        error=None,
        correlation_id=new_id(),
        lineage_classification="ORGANIC",
        scenario_run_id=None,
        scenario_step_run_id=None,
    )
    session.add(row)
    session.flush()
    return row


def _policy_config(binding_id: str) -> dict:
    return {
        "identifier": POLICY_ID,
        "name": POLICY_ID,
        "match_criteria": {"binding_id": binding_id},
        "priority": 100,
        "specificity": 100,
        "tone": "neutral",
        "initial_wait_seconds": 0,
        "allowed_disclosures": [],
        "allowed_actions": ["reminder.create"],
        "escalation_steps": [],
        "ack_timeout_seconds": 30,
        "repetition_limit": 1,
        "cancellation_conditions": ["human_reply"],
        "completion_conditions": ["safe_completion"],
    }


def _prepared(session, stamp: datetime):
    binding, actor = _owner(session)
    source = _pattern(session, actor, stamp)
    recommendation = _recommendation(source, stamp)
    persist_context_recommendation(
        session,
        recommendation=recommendation,
    )
    accepted, _ = resolve_context_recommendation(
        session,
        recommendation_id=RECOMMENDATION_ID,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        decision="ACCEPT",
        resolution_event=_accept_event(session, stamp + timedelta(minutes=1)),
    )
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    create_policy(
        session,
        POLICY_ID,
        _policy_config(binding.id),
        origin="test",
        tenant_id=DEFAULT_TENANT_ID,
    )
    grant = create_capability_grant(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        grantor_type="OPERATOR",
        grantor_id="test",
        grantee_type="ACTOR",
        grantee_id=ACTOR,
        capability_name="reminder.create",
        provenance="test",
    )
    assessment = evaluate_accepted_recommendation_authority(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        recommendation_id=RECOMMENDATION_ID,
        now=stamp + timedelta(minutes=2),
    )
    assert assessment.assessment_status == "INTENT_PREPARED"
    intent = session.get(ExecutionIntentRow, assessment.execution_intent_id)
    assert intent is not None
    return accepted, grant, assessment, intent


def _non_reminder_side_effect_counts(session) -> tuple[int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(OutboxMessageRow)),
        session.scalar(select(func.count()).select_from(AgentExecutionIntentRow)),
        session.scalar(select(func.count()).select_from(FactRow)),
    )


def test_full_revalidation_materializes_one_reminder_and_nothing_else(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    accepted, _grant, _assessment, intent = _prepared(session, stamp)

    before = _non_reminder_side_effect_counts(session)
    outcome = materialize_prepared_recommendation_execution(
        session,
        execution_intent_id=intent.id,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        recommendation_id=RECOMMENDATION_ID,
        now=stamp + timedelta(minutes=3),
    )
    after = _non_reminder_side_effect_counts(session)
    session.refresh(intent)

    assert outcome.status == "MATERIALIZED"
    assert outcome.reason_code == "REMINDER_SCHEDULED"
    assert outcome.reminder_id is not None
    assert intent.state == "MATERIALIZED"
    assert intent.frozen_at is not None

    reminder = session.get(ReminderRow, outcome.reminder_id)
    assert reminder is not None
    assert reminder.owner_actor_id == ACTOR
    assert reminder.status == "SCHEDULED"
    assert reminder.idempotency_key == intent.idempotency_key
    assert reminder.summary == "Recorrência observada pela Andy"
    assert reminder.trigger_at == stamp + timedelta(days=1)
    assert after == before

    session.refresh(accepted)
    assert accepted.object_json["execution_requested"] is False
    assert accepted.object_json["grants_authority"] is False


def test_materialized_replay_is_idempotent(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _accepted, _grant, _assessment, intent = _prepared(session, stamp)

    first = materialize_prepared_recommendation_execution(
        session,
        execution_intent_id=intent.id,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        recommendation_id=RECOMMENDATION_ID,
        now=stamp + timedelta(minutes=3),
    )
    second = materialize_prepared_recommendation_execution(
        session,
        execution_intent_id=intent.id,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        recommendation_id=RECOMMENDATION_ID,
        now=stamp + timedelta(minutes=4),
    )

    assert first.status == "MATERIALIZED"
    assert second.status == "ALREADY_MATERIALIZED"
    assert second.reminder_id == first.reminder_id
    assert session.scalar(select(func.count()).select_from(ReminderRow)) == 1


def test_revoked_grant_retires_prepared_intent_before_reminder(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _accepted, grant, _assessment, intent = _prepared(session, stamp)
    revoke_capability_grant(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        grant_id=grant.id,
    )

    outcome = materialize_prepared_recommendation_execution(
        session,
        execution_intent_id=intent.id,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        recommendation_id=RECOMMENDATION_ID,
        now=stamp + timedelta(minutes=3),
    )
    session.refresh(intent)

    assert outcome.status == "AUTHORITY_REVALIDATION_BLOCKED"
    assert outcome.reason_code == "CAPABILITY_GRANT_MISSING"
    assert intent.state == "RETIRED"
    assert intent.retired_at is not None
    assert session.scalar(select(func.count()).select_from(ReminderRow)) == 0


def test_policy_deactivation_retires_prepared_intent_before_reminder(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _accepted, _grant, _assessment, intent = _prepared(session, stamp)
    deactivate_policy(
        session,
        POLICY_ID,
        tenant_id=DEFAULT_TENANT_ID,
    )

    outcome = materialize_prepared_recommendation_execution(
        session,
        execution_intent_id=intent.id,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        recommendation_id=RECOMMENDATION_ID,
        now=stamp + timedelta(minutes=3),
    )
    session.refresh(intent)

    assert outcome.status == "AUTHORITY_REVALIDATION_BLOCKED"
    assert outcome.reason_code in {"POLICY_DENIED", "POLICY_UNRESOLVED"}
    assert intent.state == "RETIRED"
    assert session.scalar(select(func.count()).select_from(ReminderRow)) == 0


def test_provider_degradation_retires_prepared_intent_before_reminder(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _accepted, _grant, assessment, intent = _prepared(session, stamp)
    provider = session.get(ProviderInstanceRow, assessment.provider_instance_id)
    assert provider is not None
    provider.health = "DEGRADED"
    session.flush()

    outcome = materialize_prepared_recommendation_execution(
        session,
        execution_intent_id=intent.id,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        recommendation_id=RECOMMENDATION_ID,
        now=stamp + timedelta(minutes=3),
    )
    session.refresh(intent)

    assert outcome.status == "AUTHORITY_REVALIDATION_BLOCKED"
    assert outcome.reason_code == "CAPABILITY_UNAVAILABLE"
    assert intent.state == "RETIRED"
    assert session.scalar(select(func.count()).select_from(ReminderRow)) == 0


def test_expired_prepared_intent_is_retired_without_reminder(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _accepted, _grant, _assessment, intent = _prepared(session, stamp)
    intent.expires_at = stamp + timedelta(minutes=2)
    session.flush()

    outcome = materialize_prepared_recommendation_execution(
        session,
        execution_intent_id=intent.id,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        recommendation_id=RECOMMENDATION_ID,
        now=stamp + timedelta(minutes=3),
    )
    session.refresh(intent)

    assert outcome.status == "EXPIRED"
    assert intent.state == "RETIRED"
    assert session.scalar(select(func.count()).select_from(ReminderRow)) == 0


def test_prepared_scope_tamper_fails_closed_before_materialization(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _accepted, _grant, _assessment, intent = _prepared(session, stamp)
    intent.scope = {**intent.scope, "actor_id": "forged-actor"}
    session.flush()

    with pytest.raises(
        RecommendationMaterializationError,
        match="RECOMMENDATION_EXECUTION_SCOPE_FINGERPRINT_MISMATCH",
    ):
        materialize_prepared_recommendation_execution(
            session,
            execution_intent_id=intent.id,
            tenant_id=DEFAULT_TENANT_ID,
            actor_key=ACTOR,
            recommendation_id=RECOMMENDATION_ID,
            now=stamp + timedelta(minutes=3),
        )

    assert session.scalar(select(func.count()).select_from(ReminderRow)) == 0
