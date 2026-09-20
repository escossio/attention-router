from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from attention_router.application.personal_context_execution_authority import (
    EXECUTION_SCOPE_VERSION,
    evaluate_accepted_recommendation_authority,
)
from attention_router.application.personal_context_recommendation_lifecycle import (
    persist_context_recommendation,
    resolve_context_recommendation,
)
from attention_router.application.personal_context_recommendations import (
    ContextRecommendation,
)
from attention_router.application.platform.authority import create_capability_grant
from attention_router.application.platform.capability_pack import (
    provision_internal_providers,
)
from attention_router.application.platform.registry import (
    capability_and_version,
    sync_platform_registry,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    AgentExecutionIntentRow,
    AuditEventRow,
    ExecutionIntentRow,
    FactRow,
    InboundEventRow,
    MemoryActorRow,
    MemoryClaimRow,
    OutboxMessageRow,
    PolicyRow,
    ReminderRow,
    TenantRow,
)
from attention_router.infrastructure.repository import (
    create_policy,
    upsert_actor_binding,
)


ACTOR = "owner-v1e"
EXTERNAL_ACTOR = "owner-v1e-external"


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


def _install_owner(session):
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


def _pattern_claim(session, *, actor: MemoryActorRow, stamp: datetime):
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
            "hypothesis_id": "pattern-v1e",
            "snapshot_fingerprint": "sha256:v1e",
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


def _recommendation(source: MemoryClaimRow, stamp: datetime):
    return ContextRecommendation(
        recommendation_id="recommendation-v1e",
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


def _decision_event(session, *, stamp: datetime):
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
        external_event_id="v1e-accept",
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


def _accepted_recommendation(session, stamp: datetime):
    binding, actor = _install_owner(session)
    source = _pattern_claim(session, actor=actor, stamp=stamp)
    recommendation = _recommendation(source, stamp)
    persist_context_recommendation(
        session,
        recommendation=recommendation,
    )
    event = _decision_event(
        session,
        stamp=stamp + timedelta(minutes=1),
    )
    accepted, _ = resolve_context_recommendation(
        session,
        recommendation_id=recommendation.recommendation_id,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        decision="ACCEPT",
        resolution_event=event,
    )
    return binding, accepted


def _policy_config(identifier: str, binding_id: str, *, allow_reminder: bool):
    allowed = ["reminder.create"] if allow_reminder else ["state.read"]
    return {
        "identifier": identifier,
        "name": identifier,
        "match_criteria": {"binding_id": binding_id},
        "priority": 100,
        "specificity": 100,
        "tone": "neutral",
        "initial_wait_seconds": 0,
        "allowed_disclosures": [],
        "allowed_actions": allowed,
        "escalation_steps": [],
        "ack_timeout_seconds": 30,
        "repetition_limit": 1,
        "cancellation_conditions": ["human_reply"],
        "completion_conditions": ["safe_completion"],
    }


def _install_policy(session, binding_id: str, *, allow_reminder: bool = True):
    return create_policy(
        session,
        "v1e-owner-policy",
        _policy_config(
            "v1e-owner-policy",
            binding_id,
            allow_reminder=allow_reminder,
        ),
        origin="test",
        tenant_id=DEFAULT_TENANT_ID,
    )


def _side_effect_counts(session):
    return {
        "reminders": session.scalar(select(func.count()).select_from(ReminderRow)),
        "outbox": session.scalar(select(func.count()).select_from(OutboxMessageRow)),
        "agent_intents": session.scalar(
            select(func.count()).select_from(AgentExecutionIntentRow)
        ),
        "facts": session.scalar(select(func.count()).select_from(FactRow)),
    }


def test_accepted_recommendation_without_policy_prepares_no_intent(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _accepted_recommendation(session, stamp)
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    for policy in session.scalars(
        select(PolicyRow).where(
            PolicyRow.tenant_id == DEFAULT_TENANT_ID,
            PolicyRow.is_active.is_(True),
        )
    ).all():
        policy.is_active = False
    session.flush()

    assessment = evaluate_accepted_recommendation_authority(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        recommendation_id="recommendation-v1e",
        now=stamp + timedelta(minutes=2),
    )

    assert assessment.assessment_status == "POLICY_UNRESOLVED"
    assert assessment.execution_intent_id is None
    assert session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 0


def test_policy_allow_without_grant_is_denied_and_acceptance_is_not_grant(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    binding, _accepted = _accepted_recommendation(session, stamp)
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    _install_policy(session, binding.id, allow_reminder=True)

    before = _side_effect_counts(session)
    assessment = evaluate_accepted_recommendation_authority(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        recommendation_id="recommendation-v1e",
        now=stamp + timedelta(minutes=2),
    )
    after = _side_effect_counts(session)

    assert assessment.assessment_status == "DENIED"
    assert assessment.policy_allows is True
    assert assessment.active_grant_ids == ()
    assert assessment.authority_result == "DENY"
    assert assessment.reason_code == "CAPABILITY_GRANT_MISSING"
    assert assessment.execution_allowed is False
    assert assessment.execution_intent_id is None
    assert session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 0
    assert after == before


def test_policy_denial_wins_even_when_grant_exists(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    binding, _accepted = _accepted_recommendation(session, stamp)
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    _install_policy(session, binding.id, allow_reminder=False)
    create_capability_grant(
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
        recommendation_id="recommendation-v1e",
        now=stamp + timedelta(minutes=2),
    )

    assert assessment.assessment_status == "DENIED"
    assert assessment.policy_allows is False
    assert len(assessment.active_grant_ids) == 1
    assert assessment.reason_code == "POLICY_DENIED"
    assert assessment.execution_intent_id is None


def test_known_capability_without_provider_prepares_no_intent(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    binding, _accepted = _accepted_recommendation(session, stamp)
    sync_platform_registry(session)
    _install_policy(session, binding.id, allow_reminder=True)
    create_capability_grant(
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
        recommendation_id="recommendation-v1e",
        now=stamp + timedelta(minutes=2),
    )

    assert assessment.assessment_status == "CAPABILITY_UNAVAILABLE"
    assert assessment.capability_status == "KNOWN_BUT_UNAVAILABLE"
    assert assessment.authority_result == "UNAVAILABLE"
    assert assessment.execution_allowed is False
    assert assessment.execution_intent_id is None
    assert session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 0


def test_requires_approval_prepares_no_execution_intent(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    binding, _accepted = _accepted_recommendation(session, stamp)
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    _install_policy(session, binding.id, allow_reminder=True)
    create_capability_grant(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        grantor_type="OPERATOR",
        grantor_id="test",
        grantee_type="ACTOR",
        grantee_id=ACTOR,
        capability_name="reminder.create",
        provenance="test",
    )
    _definition, version = capability_and_version(
        session,
        DEFAULT_TENANT_ID,
        "reminder.create",
    )
    assert version is not None
    version.default_approval_policy = "REQUIRES_APPROVAL"
    session.flush()

    assessment = evaluate_accepted_recommendation_authority(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        recommendation_id="recommendation-v1e",
        now=stamp + timedelta(minutes=2),
    )

    assert assessment.assessment_status == "REQUIRES_APPROVAL"
    assert assessment.approval_required is True
    assert assessment.execution_allowed is False
    assert assessment.execution_intent_id is None
    assert session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 0


def test_full_current_authority_prepares_one_inert_execution_intent(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    binding, accepted = _accepted_recommendation(session, stamp)
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    _install_policy(session, binding.id, allow_reminder=True)
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

    before = _side_effect_counts(session)
    assessment = evaluate_accepted_recommendation_authority(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        recommendation_id="recommendation-v1e",
        now=stamp + timedelta(minutes=2),
    )
    after = _side_effect_counts(session)

    assert assessment.assessment_status == "INTENT_PREPARED"
    assert assessment.policy_allows is True
    assert assessment.active_grant_ids == (grant.id,)
    assert assessment.authority_result == "ALLOW"
    assert assessment.execution_allowed is True
    assert assessment.approval_required is False
    assert assessment.execution_intent_id is not None

    intent = session.get(ExecutionIntentRow, assessment.execution_intent_id)
    assert intent is not None
    assert intent.state == "PREPARED"
    assert intent.frozen_at is None
    assert intent.authority_profile_id is None
    assert intent.scope["schema_version"] == EXECUTION_SCOPE_VERSION
    assert intent.scope["tenant_id"] == DEFAULT_TENANT_ID
    assert intent.scope["actor_id"] == ACTOR
    assert intent.scope["recommendation_id"] == "recommendation-v1e"
    assert intent.scope["recommendation_claim_id"] == accepted.id
    assert intent.scope["capability"] == "reminder.create"
    assert intent.scope["grant_ids"] == [grant.id]
    assert intent.scope["parameters"]["trigger_at"] == (
        stamp + timedelta(days=1)
    ).isoformat()
    assert intent.expires_at == accepted.valid_until
    assert after == before

    current = session.get(MemoryClaimRow, accepted.id)
    assert current.object_json["execution_requested"] is False
    assert current.object_json["grants_authority"] is False


def test_authorized_assessment_is_idempotent_for_same_authority_snapshot(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    binding, _accepted = _accepted_recommendation(session, stamp)
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    _install_policy(session, binding.id, allow_reminder=True)
    create_capability_grant(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        grantor_type="OPERATOR",
        grantor_id="test",
        grantee_type="ACTOR",
        grantee_id=ACTOR,
        capability_name="reminder.create",
        provenance="test",
    )

    first = evaluate_accepted_recommendation_authority(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        recommendation_id="recommendation-v1e",
        now=stamp + timedelta(minutes=2),
    )
    second = evaluate_accepted_recommendation_authority(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        recommendation_id="recommendation-v1e",
        now=stamp + timedelta(minutes=2),
    )

    assert first.execution_intent_id == second.execution_intent_id
    assert session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 1


def test_assessment_is_audited_with_policy_and_grant_evidence(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    binding, _accepted = _accepted_recommendation(session, stamp)
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    _install_policy(session, binding.id, allow_reminder=True)
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
        recommendation_id="recommendation-v1e",
        now=stamp + timedelta(minutes=2),
    )
    audit_row = session.scalar(
        select(AuditEventRow)
        .where(
            AuditEventRow.event_type
            == "personal_context.recommendation_authority_evaluated"
        )
        .order_by(AuditEventRow.created_at.desc())
    )

    assert audit_row is not None
    assert audit_row.policy_version_id == assessment.policy_version_id
    assert audit_row.payload["active_grant_ids"] == [grant.id]
    assert audit_row.payload["assessment_status"] == "INTENT_PREPARED"
    assert audit_row.payload["execution_intent_id"] == assessment.execution_intent_id
