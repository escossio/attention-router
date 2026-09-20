from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.personal_context_anomaly_suggestions import (
    ContextAnomalySuggestion,
    SUGGESTION_CLAIM_PREDICATE,
    SUGGESTION_SOURCE_QUALITY,
)
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import MemoryActorRow, MemoryClaimRow


class ContextSuggestionPersistenceError(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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
        raise ContextSuggestionPersistenceError("SUGGESTION_ACTOR_NOT_FOUND")
    return row


def _source_anomaly(
    session: Session,
    *,
    suggestion: ContextAnomalySuggestion,
    actor: MemoryActorRow,
    now: datetime,
) -> MemoryClaimRow:
    row = session.get(MemoryClaimRow, suggestion.source_anomaly_claim_id)
    if (
        row is None
        or row.subject_actor_id != actor.id
        or row.predicate != "context.pattern.sequence_anomaly"
        or row.source_quality != "DERIVED_PATTERN"
        or row.status != "ACTIVE"
        or row.sensitivity_class == "SECRET"
        or (
            row.valid_until is not None
            and _utc(row.valid_until) <= now
        )
    ):
        raise ContextSuggestionPersistenceError(
            "SUGGESTION_SOURCE_ANOMALY_INVALID"
        )
    value = row.object_json or {}
    if (
        value.get("pattern_type") != "SEQUENCE_ANOMALY"
        or value.get("anomaly_type") != "MISSING_EXPECTED_STEP"
        or value.get("evidence_class") != "INFERRED"
        or value.get("hypothesis_status") != "HYPOTHESIS"
        or value.get("grants_authority") is not False
    ):
        raise ContextSuggestionPersistenceError(
            "SUGGESTION_SOURCE_ANOMALY_INVALID"
        )
    return row


def _source_sequence(
    session: Session,
    *,
    suggestion: ContextAnomalySuggestion,
    actor: MemoryActorRow,
    now: datetime,
) -> MemoryClaimRow:
    row = session.get(MemoryClaimRow, suggestion.source_sequence_claim_id)
    if (
        row is None
        or row.subject_actor_id != actor.id
        or row.predicate != "context.pattern.event_sequence"
        or row.source_quality != "DERIVED_PATTERN"
        or row.status != "ACTIVE"
        or row.sensitivity_class == "SECRET"
        or (
            row.valid_until is not None
            and _utc(row.valid_until) <= now
        )
    ):
        raise ContextSuggestionPersistenceError(
            "SUGGESTION_SOURCE_SEQUENCE_INVALID"
        )
    return row


def _fingerprint(suggestion: ContextAnomalySuggestion) -> str:
    return stable_hash(
        {
            "suggestion_id": suggestion.suggestion_id,
            "tenant_id": suggestion.tenant_id,
            "actor_id": suggestion.actor_id,
            "source_anomaly_claim_id": suggestion.source_anomaly_claim_id,
            "source_sequence_claim_id": suggestion.source_sequence_claim_id,
            "suggestion_type": suggestion.suggestion_type,
            "explanation": suggestion.explanation,
            "confidence": suggestion.confidence,
            "generated_at": _utc(suggestion.generated_at).isoformat(),
            "valid_until": _utc(suggestion.valid_until).isoformat(),
        }
    )


def persist_anomaly_suggestion(
    session: Session,
    *,
    suggestion: ContextAnomalySuggestion,
    now: datetime | None = None,
) -> tuple[MemoryClaimRow, bool]:
    """Persist one proposal-only suggestion without execution semantics."""

    stamp = _utc(now or suggestion.generated_at)
    if suggestion.suggestion_type != "REVIEW_MISSING_ROUTINE_STEP":
        raise ContextSuggestionPersistenceError(
            "SUGGESTION_TYPE_UNSUPPORTED"
        )
    if suggestion.status != "PROPOSED":
        raise ContextSuggestionPersistenceError(
            "SUGGESTION_STATUS_INVALID"
        )
    if suggestion.execution_requested:
        raise ContextSuggestionPersistenceError(
            "SUGGESTION_EXECUTION_FORBIDDEN"
        )
    if suggestion.grants_authority:
        raise ContextSuggestionPersistenceError(
            "SUGGESTION_AUTHORITY_FORBIDDEN"
        )
    if not suggestion.requires_user_confirmation:
        raise ContextSuggestionPersistenceError(
            "SUGGESTION_CONFIRMATION_REQUIRED"
        )
    if _utc(suggestion.valid_until) <= stamp:
        raise ContextSuggestionPersistenceError(
            "SUGGESTION_VALIDITY_INVALID"
        )

    actor = _actor(
        session,
        tenant_id=suggestion.tenant_id,
        actor_key=suggestion.actor_id,
    )
    anomaly = _source_anomaly(
        session,
        suggestion=suggestion,
        actor=actor,
        now=stamp,
    )
    sequence = _source_sequence(
        session,
        suggestion=suggestion,
        actor=actor,
        now=stamp,
    )
    if (anomaly.context or {}).get("source_sequence_claim_id") != sequence.id:
        raise ContextSuggestionPersistenceError(
            "SUGGESTION_SOURCE_CHAIN_MISMATCH"
        )

    rows = session.scalars(
        select(MemoryClaimRow).where(
            MemoryClaimRow.subject_actor_id == actor.id,
            MemoryClaimRow.predicate == SUGGESTION_CLAIM_PREDICATE,
            MemoryClaimRow.source_quality == SUGGESTION_SOURCE_QUALITY,
        )
    ).all()
    matching = [
        row
        for row in rows
        if (row.context or {}).get("suggestion_id")
        == suggestion.suggestion_id
    ]
    if len(matching) > 1:
        raise ContextSuggestionPersistenceError(
            "SUGGESTION_IDEMPOTENCY_CONFLICT"
        )

    fingerprint = _fingerprint(suggestion)
    if matching:
        row = matching[0]
        if (
            row.status == "ACTIVE"
            and (row.context or {}).get("snapshot_fingerprint")
            == fingerprint
        ):
            return row, False
        raise ContextSuggestionPersistenceError(
            "SUGGESTION_IDEMPOTENCY_CONFLICT"
        )

    row = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=actor.id,
        subject_entity_id=None,
        predicate=SUGGESTION_CLAIM_PREDICATE,
        object_type="JSON",
        object_text=suggestion.explanation,
        object_actor_id=None,
        object_entity_id=None,
        object_json={
            "suggestion_type": suggestion.suggestion_type,
            "lifecycle_state": "PROPOSED",
            "requires_user_confirmation": True,
            "execution_requested": False,
            "grants_authority": False,
            "capability_name": None,
        },
        context={
            "suggestion_id": suggestion.suggestion_id,
            "source_anomaly_claim_id": anomaly.id,
            "source_sequence_claim_id": sequence.id,
            "snapshot_fingerprint": fingerprint,
            "explanation": suggestion.explanation,
        },
        confidence=suggestion.confidence,
        sensitivity_class="PRIVATE",
        source_quality=SUGGESTION_SOURCE_QUALITY,
        valid_from=stamp,
        valid_until=_utc(suggestion.valid_until),
        status="ACTIVE",
        staleness_class="PERISHABLE",
        supersedes_claim_id=None,
        conflict_group_id=None,
        first_observed_at=stamp,
        last_observed_at=stamp,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row, True


def reconcile_anomaly_suggestions(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    now: datetime | None = None,
) -> int:
    """Expire/supersede suggestions whose inferred source chain is no longer live."""

    stamp = _utc(now or datetime.now(UTC))
    actor = session.scalar(
        select(MemoryActorRow).where(
            MemoryActorRow.tenant_id == tenant_id,
            MemoryActorRow.actor_key == actor_key,
        )
    )
    if actor is None:
        return 0

    rows = session.scalars(
        select(MemoryClaimRow).where(
            MemoryClaimRow.subject_actor_id == actor.id,
            MemoryClaimRow.predicate == SUGGESTION_CLAIM_PREDICATE,
            MemoryClaimRow.source_quality == SUGGESTION_SOURCE_QUALITY,
            MemoryClaimRow.status == "ACTIVE",
        )
    ).all()
    changed = 0
    for row in rows:
        context = row.context or {}
        anomaly = session.get(
            MemoryClaimRow,
            context.get("source_anomaly_claim_id"),
        )
        sequence = session.get(
            MemoryClaimRow,
            context.get("source_sequence_claim_id"),
        )
        reason: str | None = None
        if row.valid_until is not None and _utc(row.valid_until) <= stamp:
            reason = "SUGGESTION_EXPIRED"
        elif anomaly is None or anomaly.status != "ACTIVE":
            reason = "SOURCE_ANOMALY_INACTIVE"
        elif sequence is None or sequence.status != "ACTIVE":
            reason = "SOURCE_SEQUENCE_INACTIVE"

        if reason is None:
            continue
        row.status = "EXPIRED" if reason == "SUGGESTION_EXPIRED" else "SUPERSEDED"
        row.valid_until = (
            min(_utc(row.valid_until), stamp)
            if row.valid_until is not None
            else stamp
        )
        row.context = {
            **context,
            "terminal_reason": reason,
            "terminal_at": stamp.isoformat(),
        }
        row.updated_at = now_utc()
        changed += 1

    if changed:
        session.flush()
    return changed


__all__ = [
    "ContextSuggestionPersistenceError",
    "persist_anomaly_suggestion",
    "reconcile_anomaly_suggestions",
]
