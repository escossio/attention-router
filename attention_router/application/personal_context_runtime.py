from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.personal_context_hypotheses import (
    ContextHypothesisPersistenceError,
    persist_context_pattern_hypothesis,
)
from attention_router.application.personal_context_patterns import (
    detect_temporal_recurrence_hypotheses,
)
from attention_router.application.personal_context_sequences import (
    detect_event_sequence_hypotheses,
)
from attention_router.application.personal_context_sequence_hypotheses import (
    ContextSequencePersistenceError,
    persist_context_event_sequence_hypothesis,
)
from attention_router.application.personal_context_anomalies import (
    detect_missing_step_anomalies,
)
from attention_router.application.personal_context_anomaly_hypotheses import (
    ContextAnomalyPersistenceError,
    persist_missing_step_anomaly,
    reconcile_missing_step_anomalies,
)
from attention_router.application.personal_context_anomaly_suggestions import (
    build_anomaly_suggestions,
)
from attention_router.application.personal_context_suggestion_lifecycle import (
    ContextSuggestionPersistenceError,
    persist_anomaly_suggestion,
    reconcile_anomaly_suggestions,
)
from attention_router.application.personal_context_recommendation_lifecycle import (
    RECOMMENDATION_CLAIM_PREDICATE,
    RECOMMENDATION_SOURCE_QUALITY,
    RecommendationLifecycleError,
    expire_due_context_recommendations,
    persist_context_recommendation,
)
from attention_router.application.personal_context_recommendations import (
    ContextRecommendation,
    build_context_recommendations,
)
from attention_router.domain.enums import InteractionState
from attention_router.domain.models import new_id
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ActorBindingRow,
    InteractionRow,
    MemoryActorRow,
    MemoryClaimRow,
    OutboxMessageRow,
)
from attention_router.infrastructure.repository import audit


PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION: Final = (
    "personal_context_recommendation_text"
)
PRIMARY_OWNER_CHANNEL_ROLE: Final = "PRIMARY_OWNER_WHATSAPP"


@dataclass(frozen=True, slots=True)
class PersonalContextRuntimeCycleResult:
    owners_scanned: int = 0
    owners_failed: int = 0
    hypotheses_detected: int = 0
    hypotheses_persisted: int = 0
    sequence_hypotheses_detected: int = 0
    sequence_hypotheses_persisted: int = 0
    anomaly_hypotheses_detected: int = 0
    anomaly_hypotheses_persisted: int = 0
    anomaly_hypotheses_reconciled: int = 0
    suggestions_built: int = 0
    suggestions_persisted: int = 0
    suggestions_reconciled: int = 0
    recommendations_built: int = 0
    recommendations_persisted: int = 0
    recommendations_enqueued: int = 0
    recommendations_skipped_terminal: int = 0
    recommendations_blocked_channel: int = 0
    expired_recommendations: int = 0


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _owner_actor_scopes(
    session: Session,
) -> tuple[tuple[str, str], ...]:
    rows = session.scalars(
        select(ActorBindingRow)
        .where(
            ActorBindingRow.is_active.is_(True),
            ActorBindingRow.actor_category == "owner",
        )
        .order_by(
            ActorBindingRow.tenant_id,
            ActorBindingRow.actor_key,
            ActorBindingRow.id,
        )
    ).all()
    scopes = {
        (row.tenant_id, row.actor_key)
        for row in rows
        if (row.binding_metadata or {}).get("owner") is True
    }
    return tuple(sorted(scopes))


def _owner_delivery_binding(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
) -> ActorBindingRow | None:
    rows = session.scalars(
        select(ActorBindingRow)
        .where(
            ActorBindingRow.tenant_id == tenant_id,
            ActorBindingRow.actor_key == actor_key,
            ActorBindingRow.source == "wwebjs",
            ActorBindingRow.actor_category == "owner",
            ActorBindingRow.is_active.is_(True),
        )
        .order_by(ActorBindingRow.id)
    ).all()
    rows = [
        row
        for row in rows
        if (row.binding_metadata or {}).get("owner") is True
    ]
    preferred = [
        row
        for row in rows
        if (row.binding_metadata or {}).get("owner_channel_role")
        == PRIMARY_OWNER_CHANNEL_ROLE
    ]
    if len(preferred) == 1:
        return preferred[0]
    if preferred:
        return None
    if len(rows) == 1:
        return rows[0]
    return None


def _active_recommendation_claim(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    recommendation_id: str,
) -> MemoryClaimRow | None:
    actor_ids = session.scalars(
        select(MemoryActorRow.id).where(
            MemoryActorRow.tenant_id == tenant_id,
            MemoryActorRow.actor_key == actor_key,
        )
    ).all()
    if not actor_ids:
        return None
    rows = session.scalars(
        select(MemoryClaimRow)
        .where(
            MemoryClaimRow.subject_actor_id.in_(actor_ids),
            MemoryClaimRow.predicate == RECOMMENDATION_CLAIM_PREDICATE,
            MemoryClaimRow.source_quality == RECOMMENDATION_SOURCE_QUALITY,
            MemoryClaimRow.status == "ACTIVE",
        )
        .order_by(MemoryClaimRow.updated_at.desc(), MemoryClaimRow.id.desc())
    ).all()
    matching = [
        row
        for row in rows
        if (row.context or {}).get("recommendation_id") == recommendation_id
    ]
    if len(matching) > 1:
        raise RecommendationLifecycleError(
            "RECOMMENDATION_ACTIVE_CONFLICT"
        )
    return matching[0] if matching else None


def _recommendation_delivery_text(
    recommendation: ContextRecommendation,
) -> str:
    if recommendation.recommendation_type != "REMINDER_FOR_RECURRENT_ARRIVAL":
        raise RecommendationLifecycleError("RECOMMENDATION_TYPE_UNSUPPORTED")
    return (
        "Percebi uma recorrência de chegada com "
        f"{recommendation.occurrence_count} ocorrências. "
        "Sugestão: preparar um lembrete para a próxima ocorrência "
        "prevista. Não vou criar nada sem uma confirmação explícita."
    )


def _interaction_identity(
    recommendation_id: str,
) -> tuple[str, str]:
    digest = stable_hash(
        {
            "kind": "PERSONAL_CONTEXT_RECOMMENDATION",
            "recommendation_id": recommendation_id,
        }
    )
    return (
        f"pc-rec-{digest[:48]}",
        f"pc-rec-{digest[:40]}",
    )


def _ensure_recommendation_interaction(
    session: Session,
    *,
    recommendation: ContextRecommendation,
    recommendation_claim: MemoryClaimRow,
    binding: ActorBindingRow,
    now: datetime,
) -> InteractionRow:
    interaction_id, correlation_id = _interaction_identity(
        recommendation.recommendation_id
    )
    contact_id = (
        "personal_context:"
        + stable_hash(recommendation.actor_id)[:48]
    )
    existing = session.get(InteractionRow, interaction_id)
    if existing is not None:
        if (
            existing.tenant_id != recommendation.tenant_id
            or existing.contact_id != contact_id
            or existing.causation_id != recommendation_claim.id
        ):
            raise RecommendationLifecycleError(
                "RECOMMENDATION_INTERACTION_IDEMPOTENCY_CONFLICT"
            )
        return existing

    row = InteractionRow(
        id=interaction_id,
        tenant_id=recommendation.tenant_id,
        event_type="PERSONAL_CONTEXT_RECOMMENDATION",
        contact_id=contact_id,
        contact_name=binding.display_name or "Owner",
        relationship_category="owner",
        active_context="personal_context",
        inbound_text="",
        state=InteractionState.COMPLETED.value,
        policy_id=None,
        policy_version_id=None,
        correlation_id=correlation_id,
        causation_id=recommendation_claim.id,
        lia_speech=None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    audit(
        session,
        row.id,
        "personal_context.recommendation_interaction_created",
        {
            "recommendation_id": recommendation.recommendation_id,
            "recommendation_claim_id": recommendation_claim.id,
        },
        correlation_id=correlation_id,
        causation_id=recommendation_claim.id,
        next_state=InteractionState.COMPLETED.value,
        origin="personal_context",
        tenant_id=recommendation.tenant_id,
        created_at=now,
    )
    session.flush()
    return row


def _enqueue_recommendation(
    session: Session,
    *,
    recommendation: ContextRecommendation,
    recommendation_claim: MemoryClaimRow,
    binding: ActorBindingRow,
    now: datetime,
) -> tuple[OutboxMessageRow, bool]:
    key = (
        "personal-context:recommendation:"
        f"{recommendation.recommendation_id}"
    )
    existing = session.scalar(
        select(OutboxMessageRow).where(
            OutboxMessageRow.idempotency_key == key
        )
    )
    if existing is not None:
        if (
            existing.action_type
            != PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION
            or existing.payload.get("recommendation_id")
            != recommendation.recommendation_id
            or existing.payload.get("external_actor_id")
            != binding.external_actor_id
        ):
            raise RecommendationLifecycleError(
                "RECOMMENDATION_OUTBOX_IDEMPOTENCY_CONFLICT"
            )
        return existing, False

    interaction = _ensure_recommendation_interaction(
        session,
        recommendation=recommendation,
        recommendation_claim=recommendation_claim,
        binding=binding,
        now=now,
    )
    row = OutboxMessageRow(
        id=new_id(),
        interaction_id=interaction.id,
        action_type=PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION,
        destination="local_transport",
        payload={
            "external_actor_id": binding.external_actor_id,
            "message_type": "text",
            "text": _recommendation_delivery_text(recommendation),
            "recommendation_id": recommendation.recommendation_id,
            "recommendation_claim_id": recommendation_claim.id,
        },
        status="PENDING",
        created_at=now,
        available_at=now,
        claimed_at=None,
        claimed_by=None,
        attempt_count=0,
        last_error=None,
        completed_at=None,
        idempotency_key=key,
        correlation_id=interaction.correlation_id,
        causation_id=recommendation_claim.id,
        execution_intent_id=None,
    )
    session.add(row)
    audit(
        session,
        interaction.id,
        "personal_context.recommendation_enqueued",
        {
            "recommendation_id": recommendation.recommendation_id,
            "recommendation_claim_id": recommendation_claim.id,
            "outbox_id": row.id,
        },
        correlation_id=interaction.correlation_id,
        causation_id=recommendation_claim.id,
        origin="personal_context",
        tenant_id=recommendation.tenant_id,
        created_at=now,
    )
    session.flush()
    return row, True


def run_personal_context_runtime_cycle(
    session: Session,
    *,
    now: datetime | None = None,
    delivery_enabled: bool = False,
    owner_limit: int = 50,
) -> PersonalContextRuntimeCycleResult:
    """Run bounded Personal Context detection/persistence/recommendation work.

    This cycle never accepts a recommendation and never creates an execution
    intent or reminder.
    """

    if owner_limit < 1 or owner_limit > 500:
        raise ValueError("PERSONAL_CONTEXT_OWNER_LIMIT_OUT_OF_RANGE")
    stamp = _utc(now or datetime.now(UTC))

    expired = expire_due_context_recommendations(
        session,
        now=stamp,
        limit=500,
    )
    counters = {
        "owners_scanned": 0,
        "owners_failed": 0,
        "hypotheses_detected": 0,
        "hypotheses_persisted": 0,
        "sequence_hypotheses_detected": 0,
        "sequence_hypotheses_persisted": 0,
        "anomaly_hypotheses_detected": 0,
        "anomaly_hypotheses_persisted": 0,
        "anomaly_hypotheses_reconciled": 0,
        "suggestions_built": 0,
        "suggestions_persisted": 0,
        "suggestions_reconciled": 0,
        "recommendations_built": 0,
        "recommendations_persisted": 0,
        "recommendations_enqueued": 0,
        "recommendations_skipped_terminal": 0,
        "recommendations_blocked_channel": 0,
        "expired_recommendations": expired,
    }

    for tenant_id, actor_key in _owner_actor_scopes(session)[:owner_limit]:
        counters["owners_scanned"] += 1
        try:
            with session.begin_nested():
                hypotheses = detect_temporal_recurrence_hypotheses(
                    session,
                    tenant_id=tenant_id,
                    actor_id=actor_key,
                    now=stamp,
                )
                counters["hypotheses_detected"] += len(hypotheses)

                for hypothesis in hypotheses:
                    try:
                        _claim, changed = persist_context_pattern_hypothesis(
                            session,
                            hypothesis=hypothesis,
                            now=stamp,
                        )
                    except ContextHypothesisPersistenceError:
                        continue
                    if changed:
                        counters["hypotheses_persisted"] += 1

                sequence_hypotheses = detect_event_sequence_hypotheses(
                    session,
                    tenant_id=tenant_id,
                    actor_id=actor_key,
                    now=stamp,
                )
                counters["hypotheses_detected"] += len(sequence_hypotheses)
                counters["sequence_hypotheses_detected"] += len(
                    sequence_hypotheses
                )
                for sequence_hypothesis in sequence_hypotheses:
                    try:
                        _claim, changed = (
                            persist_context_event_sequence_hypothesis(
                                session,
                                hypothesis=sequence_hypothesis,
                                now=stamp,
                            )
                        )
                    except ContextSequencePersistenceError:
                        continue
                    if changed:
                        counters["hypotheses_persisted"] += 1
                        counters["sequence_hypotheses_persisted"] += 1

                reconciled_anomalies = reconcile_missing_step_anomalies(
                    session,
                    tenant_id=tenant_id,
                    actor_key=actor_key,
                    now=stamp,
                )
                counters["anomaly_hypotheses_reconciled"] += (
                    reconciled_anomalies
                )

                anomalies = detect_missing_step_anomalies(
                    session,
                    tenant_id=tenant_id,
                    actor_key=actor_key,
                    now=stamp,
                )
                counters["hypotheses_detected"] += len(anomalies)
                counters["anomaly_hypotheses_detected"] += len(anomalies)
                for anomaly in anomalies:
                    try:
                        _claim, changed = persist_missing_step_anomaly(
                            session,
                            anomaly=anomaly,
                            now=stamp,
                        )
                    except ContextAnomalyPersistenceError:
                        continue
                    if changed:
                        counters["hypotheses_persisted"] += 1
                        counters["anomaly_hypotheses_persisted"] += 1

                counters["suggestions_reconciled"] += (
                    reconcile_anomaly_suggestions(
                        session,
                        tenant_id=tenant_id,
                        actor_key=actor_key,
                        now=stamp,
                    )
                )
                suggestions = build_anomaly_suggestions(
                    session,
                    tenant_id=tenant_id,
                    actor_key=actor_key,
                    now=stamp,
                    limit=1,
                )
                counters["suggestions_built"] += len(suggestions)
                for suggestion in suggestions:
                    try:
                        _suggestion_claim, changed = persist_anomaly_suggestion(
                            session,
                            suggestion=suggestion,
                            now=stamp,
                        )
                    except ContextSuggestionPersistenceError:
                        continue
                    if changed:
                        counters["suggestions_persisted"] += 1

                recommendations = build_context_recommendations(
                    session,
                    tenant_id=tenant_id,
                    actor_key=actor_key,
                    now=stamp,
                    limit=1,
                )
                counters["recommendations_built"] += len(recommendations)

                for recommendation in recommendations:
                    current = _active_recommendation_claim(
                        session,
                        tenant_id=tenant_id,
                        actor_key=actor_key,
                        recommendation_id=recommendation.recommendation_id,
                    )
                    if current is None:
                        recommendation_claim, changed = (
                            persist_context_recommendation(
                                session,
                                recommendation=recommendation,
                            )
                        )
                        if changed:
                            counters["recommendations_persisted"] += 1
                    else:
                        state = (current.object_json or {}).get(
                            "lifecycle_state"
                        )
                        if state != "PROPOSED":
                            counters[
                                "recommendations_skipped_terminal"
                            ] += 1
                            continue
                        recommendation_claim = current

                    if not delivery_enabled:
                        continue

                    binding = _owner_delivery_binding(
                        session,
                        tenant_id=tenant_id,
                        actor_key=actor_key,
                    )
                    if binding is None:
                        counters["recommendations_blocked_channel"] += 1
                        audit(
                            session,
                            None,
                            "personal_context.recommendation_delivery_blocked",
                            {
                                "recommendation_id": (
                                    recommendation.recommendation_id
                                ),
                                "reason_code": (
                                    "OWNER_DELIVERY_BINDING_AMBIGUOUS"
                                ),
                            },
                            origin="personal_context",
                            tenant_id=tenant_id,
                            created_at=stamp,
                        )
                        continue

                    _outbox, enqueued = _enqueue_recommendation(
                        session,
                        recommendation=recommendation,
                        recommendation_claim=recommendation_claim,
                        binding=binding,
                        now=stamp,
                    )
                    if enqueued:
                        counters["recommendations_enqueued"] += 1
        except Exception as exc:
            counters["owners_failed"] += 1
            audit(
                session,
                None,
                "personal_context.owner_cycle_failed",
                {
                    "actor_key_hash": stable_hash(actor_key)[:16],
                    "error_class": type(exc).__name__,
                },
                origin="personal_context",
                tenant_id=tenant_id,
                created_at=stamp,
            )

    return PersonalContextRuntimeCycleResult(**counters)


__all__ = [
    "PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION",
    "PersonalContextRuntimeCycleResult",
    "run_personal_context_runtime_cycle",
]
