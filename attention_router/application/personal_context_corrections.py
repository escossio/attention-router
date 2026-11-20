from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.personal_context_hypotheses import (
    PATTERN_CLAIM_PREDICATE,
    PATTERN_CLAIM_SOURCE_QUALITY,
    PATTERN_CORRECTION_PREDICATE,
    PATTERN_CORRECTION_SOURCE_QUALITY,
    PATTERN_RELEARN_MIN_POST_CORRECTION_OCCURRENCES,
)
from attention_router.application.personal_context_recommendation_lifecycle import (
    RECOMMENDATION_CLAIM_PREDICATE,
    RECOMMENDATION_SOURCE_QUALITY,
)
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    ActorBindingRow,
    ExecutionIntentRow,
    InboundEventRow,
    MemoryActorRow,
    MemoryClaimRow,
    OutboxMessageRow,
)
from attention_router.infrastructure.repository import audit


PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION: Final = (
    "personal_context_recommendation_text"
)


class PatternCorrectionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PatternCorrectionOutcome:
    correction_claim_id: str
    corrected_source_claim_id: str
    invalidated_recommendation_ids: tuple[str, ...]
    retired_execution_intent_ids: tuple[str, ...]
    canceled_outbox_ids: tuple[str, ...]
    changed: bool


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _owner_actor(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
) -> MemoryActorRow:
    actor = session.scalar(
        select(MemoryActorRow).where(
            MemoryActorRow.tenant_id == tenant_id,
            MemoryActorRow.actor_key == actor_key,
        )
    )
    if actor is None:
        raise PatternCorrectionError("PATTERN_CORRECTION_ACTOR_NOT_FOUND")
    return actor


def _validate_owner_event(
    session: Session,
    *,
    event: InboundEventRow,
    tenant_id: str,
    actor_key: str,
) -> None:
    if event.tenant_id != tenant_id:
        raise PatternCorrectionError("PATTERN_CORRECTION_TENANT_MISMATCH")
    payload = event.payload or {}
    metadata = payload.get("metadata") or {}
    if (
        payload.get("event_origin") != "OWNER_COMMAND"
        or payload.get("owner_authenticated") is not True
        or metadata.get("from_me") is not True
        or metadata.get("owner_self_chat") is not True
        or metadata.get("from_me_classification") != "OWNER_COMMAND"
        or metadata.get("final_from_me_classification") != "OWNER_COMMAND"
    ):
        raise PatternCorrectionError(
            "PATTERN_CORRECTION_OWNER_AUTHORITY_UNAVAILABLE"
        )

    external_actor_id = payload.get("actor_id")
    if not isinstance(external_actor_id, str) or not external_actor_id:
        raise PatternCorrectionError("PATTERN_CORRECTION_ACTOR_MISSING")
    binding = session.scalar(
        select(ActorBindingRow).where(
            ActorBindingRow.tenant_id == tenant_id,
            ActorBindingRow.actor_key == actor_key,
            ActorBindingRow.source == event.source,
            ActorBindingRow.external_actor_id == external_actor_id,
            ActorBindingRow.is_active.is_(True),
        )
    )
    if binding is None:
        raise PatternCorrectionError("PATTERN_CORRECTION_ACTOR_MISMATCH")


def _existing_event_correction(
    session: Session,
    *,
    actor_id: str,
    event_id: str,
    hypothesis_id: str,
) -> MemoryClaimRow | None:
    rows = session.scalars(
        select(MemoryClaimRow).where(
            MemoryClaimRow.subject_actor_id == actor_id,
            MemoryClaimRow.predicate == PATTERN_CORRECTION_PREDICATE,
            MemoryClaimRow.source_quality == PATTERN_CORRECTION_SOURCE_QUALITY,
        )
    ).all()
    consumed = [
        row
        for row in rows
        if (row.context or {}).get("correction_inbound_event_id") == event_id
    ]
    if not consumed:
        return None
    if len(consumed) != 1:
        raise PatternCorrectionError("PATTERN_CORRECTION_EVENT_CONFLICT")
    existing = consumed[0]
    if (existing.context or {}).get("hypothesis_id") != hypothesis_id:
        raise PatternCorrectionError("PATTERN_CORRECTION_EVENT_REUSED")
    return existing


def _active_hypothesis(
    session: Session,
    *,
    actor_id: str,
    hypothesis_id: str,
) -> MemoryClaimRow:
    rows = session.scalars(
        select(MemoryClaimRow)
        .where(
            MemoryClaimRow.subject_actor_id == actor_id,
            MemoryClaimRow.predicate == PATTERN_CLAIM_PREDICATE,
            MemoryClaimRow.source_quality == PATTERN_CLAIM_SOURCE_QUALITY,
            MemoryClaimRow.status == "ACTIVE",
        )
        .order_by(MemoryClaimRow.updated_at.desc(), MemoryClaimRow.id.desc())
    ).all()
    matching = [
        row
        for row in rows
        if (row.context or {}).get("hypothesis_id") == hypothesis_id
    ]
    if len(matching) != 1:
        raise PatternCorrectionError(
            "PATTERN_CORRECTION_ACTIVE_HYPOTHESIS_NOT_UNIQUE"
        )
    return matching[0]


def _pattern_lineage_claim_ids(
    session: Session,
    *,
    actor_id: str,
    hypothesis_id: str,
) -> tuple[str, ...]:
    rows = session.scalars(
        select(MemoryClaimRow).where(
            MemoryClaimRow.subject_actor_id == actor_id,
            MemoryClaimRow.predicate == PATTERN_CLAIM_PREDICATE,
            MemoryClaimRow.source_quality == PATTERN_CLAIM_SOURCE_QUALITY,
        )
    ).all()
    return tuple(
        row.id
        for row in rows
        if (row.context or {}).get("hypothesis_id") == hypothesis_id
    )


def _invalidate_recommendations(
    session: Session,
    *,
    actor_id: str,
    source_claim_ids: tuple[str, ...],
    stamp: datetime,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    rows = session.scalars(
        select(MemoryClaimRow).where(
            MemoryClaimRow.subject_actor_id == actor_id,
            MemoryClaimRow.predicate == RECOMMENDATION_CLAIM_PREDICATE,
            MemoryClaimRow.source_quality == RECOMMENDATION_SOURCE_QUALITY,
            MemoryClaimRow.status == "ACTIVE",
        )
    ).all()
    lineage_ids = set(source_claim_ids)
    invalidated_claim_ids: list[str] = []
    recommendation_ids: list[str] = []
    for row in rows:
        context = row.context or {}
        if context.get("source_claim_id") not in lineage_ids:
            continue
        row.status = "SUPERSEDED"
        row.valid_until = (
            min(_utc(row.valid_until), stamp)
            if row.valid_until is not None
            else stamp
        )
        row.updated_at = now_utc()
        invalidated_claim_ids.append(row.id)
        recommendation_id = context.get("recommendation_id")
        if isinstance(recommendation_id, str) and recommendation_id:
            recommendation_ids.append(recommendation_id)
    return tuple(invalidated_claim_ids), tuple(recommendation_ids)


def _retire_execution_intents(
    session: Session,
    *,
    recommendation_claim_ids: tuple[str, ...],
    recommendation_ids: tuple[str, ...],
    stamp: datetime,
    tenant_id: str,
) -> tuple[str, ...]:
    if not recommendation_claim_ids and not recommendation_ids:
        return ()
    rows = session.scalars(
        select(ExecutionIntentRow).where(
            ExecutionIntentRow.state.in_(("PREPARED", "FROZEN"))
        )
    ).all()
    retired: list[str] = []
    claim_ids = set(recommendation_claim_ids)
    rec_ids = set(recommendation_ids)
    for row in rows:
        scope = row.scope or {}
        if scope.get("tenant_id") != tenant_id:
            continue
        if (
            scope.get("recommendation_claim_id") not in claim_ids
            and scope.get("recommendation_id") not in rec_ids
        ):
            continue
        row.state = "RETIRED"
        row.retired_at = stamp
        retired.append(row.id)
        audit(
            session,
            None,
            "personal_context.recommendation_execution_retired",
            {
                "recommendation_id": scope.get("recommendation_id"),
                "execution_intent_id": row.id,
                "reason_code": "PATTERN_CORRECTED_BY_OWNER",
            },
            origin="personal_context",
            tenant_id=tenant_id,
            created_at=stamp,
        )
    return tuple(retired)


def _cancel_recommendation_outbox(
    session: Session,
    *,
    recommendation_claim_ids: tuple[str, ...],
    recommendation_ids: tuple[str, ...],
    stamp: datetime,
) -> tuple[str, ...]:
    if not recommendation_claim_ids and not recommendation_ids:
        return ()
    rows = session.scalars(
        select(OutboxMessageRow).where(
            OutboxMessageRow.action_type
            == PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION,
            OutboxMessageRow.status.in_(("PENDING", "RETRY")),
        )
    ).all()
    claim_ids = set(recommendation_claim_ids)
    rec_ids = set(recommendation_ids)
    canceled: list[str] = []
    for row in rows:
        payload = row.payload or {}
        if payload.get("recommendation_claim_id") not in claim_ids:
            continue
        if payload.get("recommendation_id") not in rec_ids:
            continue
        row.status = "CANCELED"
        row.completed_at = stamp
        row.last_error = "PATTERN_CORRECTED_BY_OWNER"
        canceled.append(row.id)
    return tuple(canceled)


def correct_context_pattern_hypothesis(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    hypothesis_id: str,
    correction_event: InboundEventRow,
    now: datetime | None = None,
) -> PatternCorrectionOutcome:
    """Apply one explicit owner correction to an inferred pattern.

    The correction has no execution authority. It suppresses relearning of the
    same stable hypothesis identity until at least three qualifying timeline
    observations occur after the correction.
    """

    if not hypothesis_id:
        raise PatternCorrectionError("PATTERN_CORRECTION_HYPOTHESIS_ID_MISSING")
    stamp = _utc(now or correction_event.received_at)
    actor = _owner_actor(
        session,
        tenant_id=tenant_id,
        actor_key=actor_key,
    )
    _validate_owner_event(
        session,
        event=correction_event,
        tenant_id=tenant_id,
        actor_key=actor_key,
    )

    existing = _existing_event_correction(
        session,
        actor_id=actor.id,
        event_id=correction_event.id,
        hypothesis_id=hypothesis_id,
    )
    if existing is not None:
        context = existing.context or {}
        return PatternCorrectionOutcome(
            correction_claim_id=existing.id,
            corrected_source_claim_id=str(
                context.get("corrected_source_claim_id") or ""
            ),
            invalidated_recommendation_ids=tuple(
                context.get("invalidated_recommendation_ids") or ()
            ),
            retired_execution_intent_ids=tuple(
                context.get("retired_execution_intent_ids") or ()
            ),
            canceled_outbox_ids=tuple(
                context.get("canceled_outbox_ids") or ()
            ),
            changed=False,
        )

    source = _active_hypothesis(
        session,
        actor_id=actor.id,
        hypothesis_id=hypothesis_id,
    )
    value = source.object_json or {}
    context = source.context or {}
    if (
        value.get("evidence_class") != "INFERRED"
        or value.get("hypothesis_status") != "HYPOTHESIS"
        or value.get("grants_authority") is not False
    ):
        raise PatternCorrectionError("PATTERN_CORRECTION_SOURCE_INVALID")

    source.status = "SUPERSEDED"
    source.valid_until = (
        min(_utc(source.valid_until), stamp)
        if source.valid_until is not None
        else stamp
    )
    source.updated_at = now_utc()

    lineage_claim_ids = _pattern_lineage_claim_ids(
        session,
        actor_id=actor.id,
        hypothesis_id=hypothesis_id,
    )
    invalidated_claim_ids, recommendation_ids = _invalidate_recommendations(
        session,
        actor_id=actor.id,
        source_claim_ids=lineage_claim_ids,
        stamp=stamp,
    )
    retired_intent_ids = _retire_execution_intents(
        session,
        recommendation_claim_ids=invalidated_claim_ids,
        recommendation_ids=recommendation_ids,
        stamp=stamp,
        tenant_id=tenant_id,
    )
    canceled_outbox_ids = _cancel_recommendation_outbox(
        session,
        recommendation_claim_ids=invalidated_claim_ids,
        recommendation_ids=recommendation_ids,
        stamp=stamp,
    )

    correction = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=actor.id,
        subject_entity_id=None,
        predicate=PATTERN_CORRECTION_PREDICATE,
        object_type="JSON",
        object_text=None,
        object_actor_id=None,
        object_entity_id=None,
        object_json={
            "correction_kind": "REJECT_PATTERN",
            "pattern_type": value.get("pattern_type"),
            "event_type": value.get("event_type"),
            "signature_kind": value.get("signature_kind"),
            "signature_value": value.get("signature_value"),
            "evidence_class": "EXPLICITLY_CONFIRMED",
            "suppression_policy": (
                "RELEARN_AFTER_POST_CORRECTION_EVIDENCE"
            ),
            "required_post_correction_occurrences": (
                PATTERN_RELEARN_MIN_POST_CORRECTION_OCCURRENCES
            ),
            "grants_authority": False,
            "recommendation_ready": False,
        },
        context={
            "hypothesis_id": hypothesis_id,
            "corrected_source_claim_id": source.id,
            "correction_inbound_event_id": correction_event.id,
            "corrected_at": stamp.isoformat(),
            "source_provenance": list(
                context.get("source_provenance") or ()
            ),
            "invalidated_recommendation_ids": list(recommendation_ids),
            "invalidated_recommendation_claim_ids": list(
                invalidated_claim_ids
            ),
            "retired_execution_intent_ids": list(retired_intent_ids),
            "canceled_outbox_ids": list(canceled_outbox_ids),
        },
        confidence=1.0,
        sensitivity_class=source.sensitivity_class,
        source_quality=PATTERN_CORRECTION_SOURCE_QUALITY,
        valid_from=stamp,
        valid_until=None,
        status="ACTIVE",
        staleness_class="STABLE",
        supersedes_claim_id=source.id,
        conflict_group_id=None,
        first_observed_at=stamp,
        last_observed_at=stamp,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(correction)
    audit(
        session,
        None,
        "personal_context.pattern_corrected_by_owner",
        {
            "hypothesis_id": hypothesis_id,
            "corrected_source_claim_id": source.id,
            "correction_claim_id": correction.id,
            "correction_inbound_event_id": correction_event.id,
            "invalidated_recommendation_ids": list(recommendation_ids),
            "retired_execution_intent_ids": list(retired_intent_ids),
            "canceled_outbox_ids": list(canceled_outbox_ids),
        },
        causation_id=correction_event.id,
        origin="personal_context",
        tenant_id=tenant_id,
        created_at=stamp,
    )
    session.flush()

    return PatternCorrectionOutcome(
        correction_claim_id=correction.id,
        corrected_source_claim_id=source.id,
        invalidated_recommendation_ids=recommendation_ids,
        retired_execution_intent_ids=retired_intent_ids,
        canceled_outbox_ids=canceled_outbox_ids,
        changed=True,
    )


__all__ = [
    "PatternCorrectionError",
    "PatternCorrectionOutcome",
    "correct_context_pattern_hypothesis",
]
