from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import MemoryActorRow, MemoryClaimRow

if TYPE_CHECKING:
    from attention_router.infrastructure.models import InboundEventRow


PATTERN_CORRECTION_PREDICATE: Final = "context.pattern.owner_correction"
PATTERN_CORRECTION_SOURCE_QUALITY: Final = "USER_DECLARED"
PATTERN_CLAIM_PREDICATE: Final = "context.pattern.temporal_recurrence"
PATTERN_CLAIM_SOURCE_QUALITY: Final = "DERIVED_PATTERN"
DEFAULT_PATTERN_CORRECTION_TTL: Final = timedelta(days=30)


class ContextPatternCorrectionError(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _explicit_owner_correction_event(
    session: Session,
    *,
    event: InboundEventRow,
    tenant_id: str,
    actor_key: str,
) -> None:
    from attention_router.infrastructure.models import ActorBindingRow

    if event.tenant_id != tenant_id:
        raise ContextPatternCorrectionError("PATTERN_CORRECTION_TENANT_MISMATCH")
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
        raise ContextPatternCorrectionError(
            "PATTERN_CORRECTION_OWNER_AUTHORITY_UNAVAILABLE"
        )

    external_actor_id = payload.get("actor_id")
    if not isinstance(external_actor_id, str) or not external_actor_id:
        raise ContextPatternCorrectionError("PATTERN_CORRECTION_ACTOR_MISSING")
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
        raise ContextPatternCorrectionError("PATTERN_CORRECTION_ACTOR_MISMATCH")


def _actor(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
) -> MemoryActorRow:
    row = session.scalar(
        select(MemoryActorRow).where(
            MemoryActorRow.tenant_id == tenant_id,
            MemoryActorRow.actor_key == actor_key,
        )
    )
    if row is None:
        raise ContextPatternCorrectionError("PATTERN_CORRECTION_ACTOR_NOT_FOUND")
    return row


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
        raise ContextPatternCorrectionError(
            "PATTERN_CORRECTION_ACTIVE_HYPOTHESIS_NOT_UNIQUE"
        )
    return matching[0]


def active_pattern_correction(
    session: Session,
    *,
    actor_id: str,
    hypothesis_id: str,
    now: datetime,
) -> MemoryClaimRow | None:
    rows = session.scalars(
        select(MemoryClaimRow)
        .where(
            MemoryClaimRow.subject_actor_id == actor_id,
            MemoryClaimRow.predicate == PATTERN_CORRECTION_PREDICATE,
            MemoryClaimRow.source_quality == PATTERN_CORRECTION_SOURCE_QUALITY,
            MemoryClaimRow.status == "ACTIVE",
        )
        .order_by(MemoryClaimRow.updated_at.desc(), MemoryClaimRow.id.desc())
    ).all()
    matching = [
        row
        for row in rows
        if (row.context or {}).get("hypothesis_id") == hypothesis_id
        and row.valid_until is not None
        and _utc(row.valid_until) > _utc(now)
    ]
    if len(matching) > 1:
        raise ContextPatternCorrectionError("PATTERN_CORRECTION_ACTIVE_CONFLICT")
    return matching[0] if matching else None


def invalidate_context_pattern_hypothesis(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    hypothesis_id: str,
    correction_event: InboundEventRow,
    now: datetime | None = None,
    ttl: timedelta = DEFAULT_PATTERN_CORRECTION_TTL,
) -> tuple[MemoryClaimRow, bool]:
    """Record an explicit owner correction and suppress the inferred pattern.

    The correction is knowledge, not execution or disclosure authority. It
    supersedes the active inferred snapshot and blocks recreation of the same
    stable hypothesis identity until the bounded correction expires.
    """

    if ttl <= timedelta(0) or ttl > DEFAULT_PATTERN_CORRECTION_TTL:
        raise ContextPatternCorrectionError("PATTERN_CORRECTION_TTL_INVALID")
    stamp = _utc(now or correction_event.received_at)
    _explicit_owner_correction_event(
        session,
        event=correction_event,
        tenant_id=tenant_id,
        actor_key=actor_key,
    )
    actor = _actor(session, tenant_id=tenant_id, actor_key=actor_key)

    existing = active_pattern_correction(
        session,
        actor_id=actor.id,
        hypothesis_id=hypothesis_id,
        now=stamp,
    )
    if existing is not None:
        if (existing.context or {}).get("correction_event_id") == correction_event.id:
            return existing, False
        raise ContextPatternCorrectionError("PATTERN_CORRECTION_ALREADY_ACTIVE")

    used = session.scalars(
        select(MemoryClaimRow).where(
            MemoryClaimRow.subject_actor_id == actor.id,
            MemoryClaimRow.predicate == PATTERN_CORRECTION_PREDICATE,
            MemoryClaimRow.source_quality == PATTERN_CORRECTION_SOURCE_QUALITY,
        )
    ).all()
    if any(
        (row.context or {}).get("correction_event_id") == correction_event.id
        for row in used
    ):
        raise ContextPatternCorrectionError("PATTERN_CORRECTION_EVENT_REUSED")

    source = _active_hypothesis(
        session,
        actor_id=actor.id,
        hypothesis_id=hypothesis_id,
    )
    source.status = "SUPERSEDED"
    source.valid_until = (
        min(_utc(source.valid_until), stamp)
        if source.valid_until is not None
        else stamp
    )
    source.updated_at = now_utc()

    row = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=actor.id,
        subject_entity_id=None,
        predicate=PATTERN_CORRECTION_PREDICATE,
        object_type="JSON",
        object_text=None,
        object_actor_id=None,
        object_entity_id=None,
        object_json={
            "correction_kind": "INVALIDATE_INFERRED_PATTERN",
            "evidence_class": "USER_DECLARED",
            "grants_authority": False,
            "recommendation_ready": False,
        },
        context={
            "hypothesis_id": hypothesis_id,
            "corrected_claim_id": source.id,
            "correction_event_id": correction_event.id,
        },
        confidence=1.0,
        sensitivity_class="PRIVATE",
        source_quality=PATTERN_CORRECTION_SOURCE_QUALITY,
        valid_from=stamp,
        valid_until=stamp + ttl,
        status="ACTIVE",
        staleness_class="PERISHABLE",
        supersedes_claim_id=source.id,
        conflict_group_id=None,
        first_observed_at=stamp,
        last_observed_at=stamp,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row, True


__all__ = [
    "ContextPatternCorrectionError",
    "DEFAULT_PATTERN_CORRECTION_TTL",
    "PATTERN_CORRECTION_PREDICATE",
    "PATTERN_CORRECTION_SOURCE_QUALITY",
    "active_pattern_correction",
    "invalidate_context_pattern_hypothesis",
]