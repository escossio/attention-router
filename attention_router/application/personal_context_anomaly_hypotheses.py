from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.personal_context_anomalies import (
    ContextMissingStepAnomaly,
)
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    MemoryActorRow,
    MemoryClaimRow,
    TimelineEventRow,
)


ANOMALY_CLAIM_PREDICATE: Final = "context.pattern.sequence_anomaly"
ANOMALY_CLAIM_SOURCE_QUALITY: Final = "DERIVED_PATTERN"
SEQUENCE_CLAIM_PREDICATE: Final = "context.pattern.event_sequence"
SEQUENCE_CLAIM_SOURCE_QUALITY: Final = "DERIVED_PATTERN"


class ContextAnomalyPersistenceError(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _event_signature(row: TimelineEventRow) -> tuple[str, str] | None:
    if row.resource_id:
        return "RESOURCE", row.resource_id
    if row.relationship_id:
        return "RELATIONSHIP", row.relationship_id
    pattern_key = (row.event_ref or {}).get("pattern_key")
    if isinstance(pattern_key, str) and pattern_key.strip():
        return "PATTERN_KEY", pattern_key.strip()
    return None


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
        raise ContextAnomalyPersistenceError("ANOMALY_ACTOR_NOT_FOUND")
    return row


def _source_sequence(
    session: Session,
    *,
    actor: MemoryActorRow,
    anomaly: ContextMissingStepAnomaly,
    now: datetime,
) -> MemoryClaimRow:
    source = session.get(
        MemoryClaimRow,
        anomaly.source_sequence_claim_id,
    )
    if (
        source is None
        or source.subject_actor_id != actor.id
        or source.predicate != SEQUENCE_CLAIM_PREDICATE
        or source.source_quality != SEQUENCE_CLAIM_SOURCE_QUALITY
        or source.status != "ACTIVE"
        or source.sensitivity_class == "SECRET"
    ):
        raise ContextAnomalyPersistenceError(
            "ANOMALY_SOURCE_SEQUENCE_INVALID"
        )
    value = source.object_json or {}
    context = source.context or {}
    if (
        value.get("pattern_type") != "EVENT_SEQUENCE"
        or value.get("evidence_class") != "INFERRED"
        or value.get("hypothesis_status") != "HYPOTHESIS"
        or value.get("grants_authority") is not False
        or value.get("recommendation_ready") is not False
        or context.get("hypothesis_id") != anomaly.source_hypothesis_id
        or source.valid_until is None
        or _utc(source.valid_until) <= now
    ):
        raise ContextAnomalyPersistenceError(
            "ANOMALY_SOURCE_SEQUENCE_INVALID"
        )
    return source


def _first_event(
    session: Session,
    *,
    anomaly: ContextMissingStepAnomaly,
) -> TimelineEventRow:
    row = session.get(TimelineEventRow, anomaly.first_event_id)
    if (
        row is None
        or row.tenant_id != anomaly.tenant_id
        or row.actor_id != anomaly.actor_id
        or row.visibility == "SECRET"
        or row.event_type != anomaly.first_event_type
        or _event_signature(row)
        != (
            anomaly.first_signature_kind,
            anomaly.first_signature_value,
        )
    ):
        raise ContextAnomalyPersistenceError(
            "ANOMALY_FIRST_EVENT_INVALID"
        )
    return row


def _expected_step_exists(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    after: datetime,
    through: datetime,
    event_type: str,
    signature_kind: str,
    signature_value: str,
) -> TimelineEventRow | None:
    rows = session.scalars(
        select(TimelineEventRow)
        .where(
            TimelineEventRow.tenant_id == tenant_id,
            TimelineEventRow.actor_id == actor_key,
            TimelineEventRow.occurred_at > after,
            TimelineEventRow.occurred_at <= through,
            TimelineEventRow.visibility != "SECRET",
        )
        .order_by(
            TimelineEventRow.occurred_at,
            TimelineEventRow.id,
        )
    ).all()
    for row in rows:
        if (
            row.event_type == event_type
            and _event_signature(row)
            == (signature_kind, signature_value)
        ):
            return row
    return None


def persist_missing_step_anomaly(
    session: Session,
    *,
    anomaly: ContextMissingStepAnomaly,
    now: datetime | None = None,
) -> tuple[MemoryClaimRow, bool]:
    """Persist one absence-based anomaly without modifying its source routine."""

    stamp = _utc(now or datetime.now(UTC))
    if anomaly.anomaly_type != "MISSING_EXPECTED_STEP":
        raise ContextAnomalyPersistenceError(
            "ANOMALY_TYPE_UNSUPPORTED"
        )
    if (
        anomaly.evidence_class != "INFERRED"
        or anomaly.status != "HYPOTHESIS"
        or anomaly.grants_authority
        or anomaly.recommendation_ready
    ):
        raise ContextAnomalyPersistenceError(
            "ANOMALY_SEMANTIC_CLASS_INVALID"
        )
    if _utc(anomaly.window_closed_at) >= stamp:
        raise ContextAnomalyPersistenceError(
            "ANOMALY_WINDOW_NOT_CLOSED"
        )
    if _utc(anomaly.valid_until) <= stamp:
        raise ContextAnomalyPersistenceError("ANOMALY_EXPIRED")

    actor = _actor(
        session,
        tenant_id=anomaly.tenant_id,
        actor_key=anomaly.actor_id,
    )
    source = _source_sequence(
        session,
        actor=actor,
        anomaly=anomaly,
        now=stamp,
    )
    first = _first_event(session, anomaly=anomaly)
    if (
        source.last_observed_at is None
        or _utc(first.occurred_at) <= _utc(source.last_observed_at)
    ):
        raise ContextAnomalyPersistenceError(
            "ANOMALY_FIRST_EVENT_NOT_POST_SEQUENCE"
        )

    if _expected_step_exists(
        session,
        tenant_id=anomaly.tenant_id,
        actor_key=anomaly.actor_id,
        after=_utc(first.occurred_at),
        through=_utc(anomaly.window_closed_at),
        event_type=anomaly.expected_second_event_type,
        signature_kind=anomaly.expected_second_signature_kind,
        signature_value=anomaly.expected_second_signature_value,
    ) is not None:
        raise ContextAnomalyPersistenceError(
            "ANOMALY_EXPECTED_STEP_PRESENT"
        )

    rows = session.scalars(
        select(MemoryClaimRow).where(
            MemoryClaimRow.subject_actor_id == actor.id,
            MemoryClaimRow.predicate == ANOMALY_CLAIM_PREDICATE,
            MemoryClaimRow.source_quality == ANOMALY_CLAIM_SOURCE_QUALITY,
        )
    ).all()
    matching = [
        row
        for row in rows
        if (row.context or {}).get("anomaly_id") == anomaly.anomaly_id
    ]
    if len(matching) > 1:
        raise ContextAnomalyPersistenceError(
            "ANOMALY_IDEMPOTENCY_CONFLICT"
        )
    if matching:
        row = matching[0]
        if (
            row.context or {}
        ).get("source_sequence_claim_id") != source.id:
            raise ContextAnomalyPersistenceError(
                "ANOMALY_IDEMPOTENCY_CONFLICT"
            )
        return row, False

    row = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=actor.id,
        subject_entity_id=None,
        predicate=ANOMALY_CLAIM_PREDICATE,
        object_type="JSON",
        object_text=None,
        object_actor_id=None,
        object_entity_id=None,
        object_json={
            "pattern_type": "SEQUENCE_ANOMALY",
            "anomaly_type": anomaly.anomaly_type,
            "expected_second_event_type": (
                anomaly.expected_second_event_type
            ),
            "expected_second_signature_kind": (
                anomaly.expected_second_signature_kind
            ),
            "expected_second_signature_value": (
                anomaly.expected_second_signature_value
            ),
            "expected_gap_seconds": anomaly.expected_gap_seconds,
            "evidence_class": "INFERRED",
            "hypothesis_status": "HYPOTHESIS",
            "grants_authority": False,
            "recommendation_ready": False,
        },
        context={
            "anomaly_id": anomaly.anomaly_id,
            "source_sequence_claim_id": source.id,
            "source_hypothesis_id": anomaly.source_hypothesis_id,
            "first_event_id": first.id,
            "first_event_type": anomaly.first_event_type,
            "first_signature_kind": anomaly.first_signature_kind,
            "first_signature_value": anomaly.first_signature_value,
            "window_closed_at": _utc(
                anomaly.window_closed_at
            ).isoformat(),
            "detection_basis": "ABSENCE_WITHIN_EXPECTED_WINDOW",
            "source_sequence_confidence": (
                anomaly.source_sequence_confidence
            ),
            "source_sequence_support_ratio": (
                anomaly.source_sequence_support_ratio
            ),
            "source_sequence_occurrence_count": (
                anomaly.source_sequence_occurrence_count
            ),
        },
        confidence=anomaly.confidence,
        sensitivity_class="PRIVATE",
        source_quality=ANOMALY_CLAIM_SOURCE_QUALITY,
        valid_from=_utc(anomaly.window_closed_at),
        valid_until=_utc(anomaly.valid_until),
        status="ACTIVE",
        staleness_class="PERISHABLE",
        supersedes_claim_id=None,
        conflict_group_id=None,
        first_observed_at=_utc(first.occurred_at),
        last_observed_at=_utc(anomaly.window_closed_at),
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row, True


def reconcile_missing_step_anomalies(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    now: datetime | None = None,
) -> int:
    """Supersede absence hypotheses contradicted by newly arrived evidence."""

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
            MemoryClaimRow.predicate == ANOMALY_CLAIM_PREDICATE,
            MemoryClaimRow.source_quality == ANOMALY_CLAIM_SOURCE_QUALITY,
            MemoryClaimRow.status == "ACTIVE",
        )
    ).all()
    superseded = 0

    for row in rows:
        value = row.object_json or {}
        context = row.context or {}
        source_claim_id = context.get("source_sequence_claim_id")
        first_event_id = context.get("first_event_id")
        window_closed_raw = context.get("window_closed_at")
        if not all(
            isinstance(item, str) and item
            for item in (
                source_claim_id,
                first_event_id,
                window_closed_raw,
                value.get("expected_second_event_type"),
                value.get("expected_second_signature_kind"),
                value.get("expected_second_signature_value"),
            )
        ):
            continue

        source = session.get(MemoryClaimRow, source_claim_id)
        first = session.get(TimelineEventRow, first_event_id)
        reason: str | None = None
        evidence_id: str | None = None
        if (
            source is None
            or source.status != "ACTIVE"
            or source.predicate != SEQUENCE_CLAIM_PREDICATE
        ):
            reason = "SOURCE_SEQUENCE_INACTIVE"
        elif first is None:
            reason = "FIRST_EVENT_UNAVAILABLE"
        else:
            try:
                window_closed_at = datetime.fromisoformat(
                    window_closed_raw.replace("Z", "+00:00")
                )
            except ValueError:
                continue
            expected = _expected_step_exists(
                session,
                tenant_id=tenant_id,
                actor_key=actor_key,
                after=_utc(first.occurred_at),
                through=_utc(window_closed_at),
                event_type=value["expected_second_event_type"],
                signature_kind=value[
                    "expected_second_signature_kind"
                ],
                signature_value=value[
                    "expected_second_signature_value"
                ],
            )
            if expected is not None:
                reason = "EXPECTED_STEP_EVIDENCE_ARRIVED"
                evidence_id = expected.id

        if reason is None:
            continue

        row.status = "SUPERSEDED"
        row.valid_until = (
            min(_utc(row.valid_until), stamp)
            if row.valid_until is not None
            else stamp
        )
        row.context = {
            **context,
            "superseded_reason": reason,
            "superseded_at": stamp.isoformat(),
            "superseding_timeline_event_id": evidence_id,
        }
        row.updated_at = now_utc()
        superseded += 1

    if superseded:
        session.flush()
    return superseded


__all__ = [
    "ANOMALY_CLAIM_PREDICATE",
    "ANOMALY_CLAIM_SOURCE_QUALITY",
    "ContextAnomalyPersistenceError",
    "persist_missing_step_anomaly",
    "reconcile_missing_step_anomalies",
]
