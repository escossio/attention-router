from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.personal_context_hypotheses import (
    persist_context_pattern_hypothesis,
)
from attention_router.application.personal_context_patterns import (
    detect_temporal_recurrence_hypotheses,
)
from attention_router.application.personal_context_recommendation_lifecycle import (
    persist_context_recommendation,
)
from attention_router.application.personal_context_recommendations import (
    build_context_recommendations,
)
from attention_router.domain.models import ContactIdentity, new_id, now_utc
from attention_router.domain.states import InteractionState
from attention_router.infrastructure.models import (
    ActorBindingRow,
    MemoryActorRow,
    OutboxMessageRow,
)
from attention_router.infrastructure.repository import (
    audit,
    create_interaction_row,
    set_state,
)


PERSONAL_CONTEXT_RECOMMENDATION_ACTION: Final = "personal_context_recommendation"
PERSONAL_CONTEXT_RECOMMENDATION_DESTINATION: Final = "local_transport"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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
            ActorBindingRow.actor_category == "owner",
            ActorBindingRow.is_active.is_(True),
        )
        .order_by(ActorBindingRow.created_at, ActorBindingRow.id)
    ).all()
    eligible = [
        row
        for row in rows
        if row.source == "wwebjs"
        and (row.binding_metadata or {}).get("owner") is True
    ]
    return eligible[0] if len(eligible) == 1 else None


def _recommendation_outbox_key(recommendation_id: str) -> str:
    return f"personal-context:recommendation:{recommendation_id}"


def _enqueue_recommendation(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    recommendation,
) -> bool:
    idempotency_key = _recommendation_outbox_key(
        recommendation.recommendation_id
    )
    if session.scalar(
        select(OutboxMessageRow.id).where(
            OutboxMessageRow.idempotency_key == idempotency_key
        )
    ):
        return False

    binding = _owner_delivery_binding(
        session,
        tenant_id=tenant_id,
        actor_key=actor_key,
    )
    if binding is None:
        audit(
            session,
            None,
            "personal_context.recommendation_delivery_skipped",
            {
                "recommendation_id": recommendation.recommendation_id,
                "reason_code": "OWNER_DELIVERY_BINDING_UNAVAILABLE",
            },
            origin="personal_context",
            tenant_id=tenant_id,
        )
        return False

    correlation_id = new_id()
    contact = ContactIdentity(
        binding.external_actor_id,
        binding.display_name or "Owner",
        "owner",
    )
    interaction = create_interaction_row(
        session,
        "PERSONAL_CONTEXT_RECOMMENDATION",
        contact,
        "personal_context",
        "",
        correlation_id,
        recommendation.source_claim_id,
        tenant_id,
    )
    set_state(interaction, InteractionState.COMPLETED)

    stamp = now_utc()
    outbox = OutboxMessageRow(
        id=new_id(),
        interaction_id=interaction.id,
        action_type=PERSONAL_CONTEXT_RECOMMENDATION_ACTION,
        destination=PERSONAL_CONTEXT_RECOMMENDATION_DESTINATION,
        payload={
            "external_actor_id": binding.external_actor_id,
            "message_type": "text",
            "text": recommendation.explanation,
            "recommendation_id": recommendation.recommendation_id,
            "recommendation_type": recommendation.recommendation_type,
            "requires_user_confirmation": True,
        },
        status="PENDING",
        created_at=stamp,
        available_at=stamp,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        causation_id=recommendation.source_claim_id,
        execution_intent_id=None,
    )
    session.add(outbox)
    audit(
        session,
        interaction.id,
        "personal_context.recommendation_enqueued",
        {
            "recommendation_id": recommendation.recommendation_id,
            "outbox_id": outbox.id,
            "requires_user_confirmation": True,
        },
        correlation_id=correlation_id,
        causation_id=recommendation.source_claim_id,
        origin="personal_context",
        tenant_id=tenant_id,
    )
    session.flush()
    return True


def process_personal_context_recommendations(
    session: Session,
    *,
    now: datetime | None = None,
    actor_limit: int = 50,
) -> int:
    """Advance Personal Context A-D to a delivered proposal, never execution.

    The pump detects/persists recurrence, derives/persists recommendations and
    enqueues only newly persisted proposals. It never accepts a proposal,
    evaluates execution authority, creates ExecutionIntentRow or materializes a
    reminder.
    """

    if actor_limit < 1 or actor_limit > 500:
        raise ValueError("PERSONAL_CONTEXT_ACTOR_LIMIT_OUT_OF_RANGE")

    stamp = _utc(now or datetime.now(UTC))
    actors = session.scalars(
        select(MemoryActorRow)
        .order_by(MemoryActorRow.tenant_id, MemoryActorRow.actor_key)
        .limit(actor_limit)
    ).all()

    enqueued = 0
    for actor in actors:
        hypotheses = detect_temporal_recurrence_hypotheses(
            session,
            tenant_id=actor.tenant_id,
            actor_id=actor.actor_key,
            now=stamp,
        )
        for hypothesis in hypotheses:
            persist_context_pattern_hypothesis(
                session,
                hypothesis=hypothesis,
                now=stamp,
            )

        recommendations = build_context_recommendations(
            session,
            tenant_id=actor.tenant_id,
            actor_key=actor.actor_key,
            now=stamp,
        )
        for recommendation in recommendations:
            _row, changed = persist_context_recommendation(
                session,
                recommendation=recommendation,
            )
            if not changed:
                continue
            if _enqueue_recommendation(
                session,
                tenant_id=actor.tenant_id,
                actor_key=actor.actor_key,
                recommendation=recommendation,
            ):
                enqueued += 1

    return enqueued


__all__ = [
    "PERSONAL_CONTEXT_RECOMMENDATION_ACTION",
    "process_personal_context_recommendations",
]
