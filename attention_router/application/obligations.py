"""Governed obligation and expectation model for Personal Context V2G."""

from __future__ import annotations

from calendar import monthrange
from datetime import UTC, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.candidate_insight_models import CandidateInsightRow
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ActorBindingRow,
    MemoryActorRow,
    RelationshipRow,
    ResourceRow,
    TenantRow,
    TimelineEventRow,
)
from attention_router.infrastructure.obligation_models import (
    ObligationFulfillmentRow,
    ObligationInstanceRow,
    ObligationTransitionRow,
    RecurringObligationDefinitionRow,
)


MAX_GRACE_SECONDS: Final = 30 * 24 * 60 * 60
SUPPORTED_SUBJECT_TYPES: Final = {"RESOURCE", "RELATIONSHIP"}
SUPPORTED_SOURCE_KINDS: Final = {"OWNER_DECLARED", "ADMITTED_CANDIDATE"}
_SENSITIVITY_ORDER: Final = {"NORMAL": 0, "PRIVATE": 1, "SECRET": 2}


class ObligationError(RuntimeError):
    pass


def _utc(value: datetime | None = None) -> datetime:
    stamp = value or now_utc()
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def _bounded(value: str, *, code: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise ObligationError(f"{code}_REQUIRED")
    if len(normalized) > limit:
        raise ObligationError(f"{code}_TOO_LONG")
    return normalized


def _confidence(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
        or value > 1
    ):
        raise ObligationError("OBLIGATION_CONFIDENCE_INVALID")
    return float(value)


def _sensitivity(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in _SENSITIVITY_ORDER:
        raise ObligationError("OBLIGATION_SENSITIVITY_INVALID")
    return normalized


def _max_sensitivity(first: str, second: str) -> str:
    left = _sensitivity(first)
    right = _sensitivity(second)
    return max(
        (left, right),
        key=lambda item: _SENSITIVITY_ORDER[item],
    )


def _timezone(value: str) -> ZoneInfo:
    name = _bounded(value, code="OBLIGATION_TIMEZONE", limit=64)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ObligationError("OBLIGATION_TIMEZONE_INVALID") from exc


def _require_owner(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
) -> str:
    decision_actor = _bounded(
        actor_key,
        code="OBLIGATION_DECISION_ACTOR",
        limit=120,
    )
    bindings = list(
        session.scalars(
            select(ActorBindingRow).where(
                ActorBindingRow.tenant_id == tenant_id,
                ActorBindingRow.is_active.is_(True),
                or_(
                    ActorBindingRow.actor_category == "owner",
                    ActorBindingRow.binding_metadata["owner"]
                    .as_boolean()
                    .is_(True),
                ),
            )
        ).all()
    )
    owners = {row.actor_key for row in bindings}
    if len(owners) != 1:
        raise ObligationError("OBLIGATION_OWNER_AMBIGUOUS")
    if decision_actor not in owners:
        raise ObligationError("OBLIGATION_OWNER_AUTHORITY_REQUIRED")
    return decision_actor


def _subject_exists(
    session: Session,
    *,
    tenant_id: str,
    subject_type: str,
    subject_id: str,
) -> bool:
    if subject_type == "RESOURCE":
        row = session.get(ResourceRow, subject_id)
        return bool(row and row.tenant_id == tenant_id)
    if subject_type == "RELATIONSHIP":
        row = session.get(RelationshipRow, subject_id)
        return bool(row and row.tenant_id == tenant_id)
    return False


def _actor_exists(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
) -> bool:
    binding = session.scalar(
        select(ActorBindingRow.id).where(
            ActorBindingRow.tenant_id == tenant_id,
            ActorBindingRow.actor_key == actor_key,
            ActorBindingRow.is_active.is_(True),
        )
    )
    memory = session.scalar(
        select(MemoryActorRow.id).where(
            MemoryActorRow.tenant_id == tenant_id,
            MemoryActorRow.actor_key == actor_key,
        )
    )
    return bool(binding or memory)


def _transition(
    session: Session,
    *,
    instance: ObligationInstanceRow,
    from_state: str | None,
    to_state: str,
    reason_code: str,
    decision_actor_key: str | None = None,
    decision_ref: str | None = None,
    evidence_ref: str | None = None,
    metadata: dict | None = None,
    now: datetime | None = None,
) -> ObligationTransitionRow:
    row = ObligationTransitionRow(
        id=new_id(),
        tenant_id=instance.tenant_id,
        instance_id=instance.id,
        from_state=from_state,
        to_state=to_state,
        reason_code=reason_code,
        decision_actor_key=decision_actor_key,
        decision_ref=decision_ref,
        evidence_ref=evidence_ref,
        metadata_json=metadata or {},
        created_at=_utc(now),
    )
    session.add(row)
    return row


def create_recurring_obligation_definition(
    session: Session,
    *,
    tenant_id: str,
    subject_type: str,
    subject_id: str,
    obligation_kind: str,
    expected_actor_key: str,
    expected_event_type: str,
    due_day: int,
    due_timezone: str,
    grace_seconds: int = 0,
    value_constraints: dict | None = None,
    confidence: float = 1.0,
    sensitivity_class: str = "PRIVATE",
    source_kind: str = "OWNER_DECLARED",
    source_ref: str | None = None,
    decision_actor_key: str | None = None,
    decision_ref: str | None = None,
    supersedes_definition_id: str | None = None,
    provenance: dict | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    now: datetime | None = None,
) -> tuple[RecurringObligationDefinitionRow, bool]:
    """Create one governed monthly obligation definition.

    V0 supports only monthly cadence with due days 1..28. A definition is
    knowledge, never action authority.
    """
    if session.get(TenantRow, tenant_id) is None:
        raise ObligationError("OBLIGATION_TENANT_NOT_FOUND")

    subject_kind = subject_type.strip().upper()
    if subject_kind not in SUPPORTED_SUBJECT_TYPES:
        raise ObligationError("OBLIGATION_SUBJECT_TYPE_UNSUPPORTED")
    subject = _bounded(subject_id, code="OBLIGATION_SUBJECT", limit=120)
    if not _subject_exists(
        session,
        tenant_id=tenant_id,
        subject_type=subject_kind,
        subject_id=subject,
    ):
        raise ObligationError("OBLIGATION_SUBJECT_NOT_FOUND")

    kind = _bounded(obligation_kind, code="OBLIGATION_KIND", limit=80).upper()
    actor = _bounded(
        expected_actor_key,
        code="OBLIGATION_EXPECTED_ACTOR",
        limit=120,
    )
    if not _actor_exists(session, tenant_id=tenant_id, actor_key=actor):
        raise ObligationError("OBLIGATION_EXPECTED_ACTOR_NOT_FOUND")
    event_type = _bounded(
        expected_event_type,
        code="OBLIGATION_EXPECTED_EVENT",
        limit=80,
    ).upper()

    if isinstance(due_day, bool) or not isinstance(due_day, int) or not 1 <= due_day <= 28:
        raise ObligationError("OBLIGATION_DUE_DAY_INVALID")
    _timezone(due_timezone)
    if (
        isinstance(grace_seconds, bool)
        or not isinstance(grace_seconds, int)
        or grace_seconds < 0
        or grace_seconds > MAX_GRACE_SECONDS
    ):
        raise ObligationError("OBLIGATION_GRACE_INVALID")

    score = _confidence(confidence)
    sensitivity = _sensitivity(sensitivity_class)
    source = source_kind.strip().upper()
    if source not in SUPPORTED_SOURCE_KINDS:
        raise ObligationError("OBLIGATION_SOURCE_KIND_UNSUPPORTED")

    source_reference = (
        _bounded(source_ref, code="OBLIGATION_SOURCE_REF", limit=160)
        if source_ref is not None
        else None
    )
    source_provenance: dict = {}

    if source == "OWNER_DECLARED":
        if decision_actor_key is None or decision_ref is None:
            raise ObligationError("OBLIGATION_OWNER_DECISION_REQUIRED")
        owner = _require_owner(
            session,
            tenant_id=tenant_id,
            actor_key=decision_actor_key,
        )
        reference = _bounded(
            decision_ref,
            code="OBLIGATION_DECISION_REF",
            limit=240,
        )
        source_provenance = {
            "owner_actor_key": owner,
            "owner_decision_ref": reference,
        }
    else:
        if source_reference is None:
            raise ObligationError("OBLIGATION_CANDIDATE_REF_REQUIRED")
        candidate = session.get(CandidateInsightRow, source_reference)
        if (
            candidate is None
            or candidate.tenant_id != tenant_id
            or candidate.state != "ADMITTED"
            or candidate.subject_type != subject_kind
            or candidate.subject_id != subject
        ):
            raise ObligationError("OBLIGATION_CANDIDATE_NOT_ADMITTED")
        score = min(score, candidate.confidence)
        sensitivity = _max_sensitivity(
            sensitivity,
            candidate.sensitivity_class,
        )
        source_provenance = {
            "candidate_insight_id": candidate.id,
            "candidate_semantic_key": candidate.semantic_key,
        }

    stamp = _utc(now)
    start = _utc(valid_from) if valid_from is not None else stamp
    end = _utc(valid_until) if valid_until is not None else None
    if end is not None and end < start:
        raise ObligationError("OBLIGATION_VALIDITY_INVALID")

    semantic_key = (
        "obligation:"
        + stable_hash(
            {
                "tenant_id": tenant_id,
                "subject_type": subject_kind,
                "subject_id": subject,
                "obligation_kind": kind,
                "expected_actor_key": actor,
                "expected_event_type": event_type,
            }
        )[:96]
    )
    snapshot = {
        "semantic_key": semantic_key,
        "due_day": due_day,
        "due_timezone": due_timezone,
        "grace_seconds": grace_seconds,
        "value_constraints": value_constraints or {},
        "confidence": score,
        "sensitivity_class": sensitivity,
        "source_kind": source,
        "source_ref": source_reference,
        "valid_from": start.isoformat(),
        "valid_until": end.isoformat() if end else None,
    }
    idempotency_key = "obligation-definition:" + stable_hash(snapshot)[:96]

    existing = session.scalar(
        select(RecurringObligationDefinitionRow).where(
            RecurringObligationDefinitionRow.tenant_id == tenant_id,
            RecurringObligationDefinitionRow.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing, False

    active_query = select(RecurringObligationDefinitionRow).where(
        RecurringObligationDefinitionRow.tenant_id == tenant_id,
        RecurringObligationDefinitionRow.semantic_key == semantic_key,
        RecurringObligationDefinitionRow.state == "ACTIVE",
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        active_query = active_query.with_for_update()
    active_same = list(session.scalars(active_query).all())
    previous: RecurringObligationDefinitionRow | None = None
    if active_same:
        if len(active_same) != 1:
            raise ObligationError("OBLIGATION_ACTIVE_DEFINITION_CONFLICT")
        previous = active_same[0]
        if supersedes_definition_id != previous.id:
            raise ObligationError("OBLIGATION_SUPERSESSION_REQUIRED")
        if decision_actor_key is None or decision_ref is None:
            raise ObligationError("OBLIGATION_SUPERSESSION_DECISION_REQUIRED")
        owner = _require_owner(
            session,
            tenant_id=tenant_id,
            actor_key=decision_actor_key,
        )
        reference = _bounded(
            decision_ref,
            code="OBLIGATION_DECISION_REF",
            limit=240,
        )
        previous.state = "SUPERSEDED"
        previous.valid_until = min(
            _utc(previous.valid_until) if previous.valid_until is not None else stamp,
            stamp,
        )
        previous.updated_at = stamp
        session.flush()
        source_provenance = {
            **source_provenance,
            "supersession_owner_actor_key": owner,
            "supersession_decision_ref": reference,
        }
    elif supersedes_definition_id is not None:
        raise ObligationError("OBLIGATION_SUPERSEDED_DEFINITION_NOT_ACTIVE")

    row = RecurringObligationDefinitionRow(
        id=new_id(),
        tenant_id=tenant_id,
        semantic_key=semantic_key,
        idempotency_key=idempotency_key,
        subject_type=subject_kind,
        subject_id=subject,
        obligation_kind=kind,
        expected_actor_key=actor,
        expected_event_type=event_type,
        value_constraints=value_constraints or {},
        cadence_kind="MONTHLY",
        due_day=due_day,
        due_timezone=due_timezone,
        grace_seconds=grace_seconds,
        confidence=score,
        sensitivity_class=sensitivity,
        source_kind=source,
        source_ref=source_reference,
        provenance={
            **(provenance or {}),
            **source_provenance,
            "grants_authority": False,
            "grants_disclosure_authority": False,
        },
        state="ACTIVE",
        supersedes_definition_id=previous.id if previous is not None else None,
        valid_from=start,
        valid_until=end,
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    session.flush()
    return row, True


def _month_bounds(
    *,
    year: int,
    month: int,
    timezone_name: str,
    due_day: int,
    grace_seconds: int,
) -> tuple[datetime, datetime, datetime, datetime]:
    if year < 2000 or year > 2200 or month < 1 or month > 12:
        raise ObligationError("OBLIGATION_PERIOD_INVALID")
    tz = _timezone(timezone_name)
    period_start_local = datetime(year, month, 1, tzinfo=tz)
    if month == 12:
        next_start_local = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        next_start_local = datetime(year, month + 1, 1, tzinfo=tz)
    last_day = monthrange(year, month)[1]
    if due_day > last_day:
        raise ObligationError("OBLIGATION_DUE_DAY_OUTSIDE_PERIOD")
    expected_by_local = datetime(
        year,
        month,
        due_day,
        23,
        59,
        59,
        999999,
        tzinfo=tz,
    )
    period_start = _utc(period_start_local)
    period_end = _utc(next_start_local)
    expected_by = _utc(expected_by_local)
    due_window_end = min(
        expected_by + timedelta(seconds=grace_seconds),
        period_end - timedelta(microseconds=1),
    )
    return period_start, period_end, expected_by, due_window_end


def generate_monthly_obligation_instance(
    session: Session,
    *,
    definition_id: str,
    year: int,
    month: int,
    now: datetime | None = None,
) -> tuple[ObligationInstanceRow, bool]:
    definition = session.get(RecurringObligationDefinitionRow, definition_id)
    if definition is None:
        raise ObligationError("OBLIGATION_DEFINITION_NOT_FOUND")
    if definition.state != "ACTIVE":
        raise ObligationError("OBLIGATION_DEFINITION_NOT_ACTIVE")

    period_start, period_end, expected_by, due_window_end = _month_bounds(
        year=year,
        month=month,
        timezone_name=definition.due_timezone,
        due_day=definition.due_day,
        grace_seconds=definition.grace_seconds,
    )
    if _utc(definition.valid_from) > expected_by:
        raise ObligationError("OBLIGATION_PERIOD_BEFORE_VALIDITY")
    if (
        definition.valid_until is not None
        and _utc(definition.valid_until) <= expected_by
    ):
        raise ObligationError("OBLIGATION_PERIOD_AFTER_VALIDITY")

    period_key = f"{year:04d}-{month:02d}"
    existing = session.scalar(
        select(ObligationInstanceRow).where(
            ObligationInstanceRow.definition_id == definition.id,
            ObligationInstanceRow.period_key == period_key,
        )
    )
    if existing is not None:
        return existing, False

    stamp = _utc(now)
    row = ObligationInstanceRow(
        id=new_id(),
        tenant_id=definition.tenant_id,
        definition_id=definition.id,
        period_key=period_key,
        period_start=period_start,
        period_end=period_end,
        expected_by=expected_by,
        due_window_end=due_window_end,
        state="EXPECTED",
        reconciliation_status="PENDING",
        uncertainty_code="AWAITING_CONFIRMING_EVIDENCE",
        expected_value=dict(definition.value_constraints or {}),
        satisfaction_ratio=0.0,
        sensitivity_class=definition.sensitivity_class,
        extension_until=None,
        waived_at=None,
        supersedes_instance_id=None,
        provenance={
            "definition_semantic_key": definition.semantic_key,
            "generation": "MONTHLY_V0",
            "absence_is_fact": False,
            "grants_authority": False,
        },
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    session.flush()
    _transition(
        session,
        instance=row,
        from_state=None,
        to_state="EXPECTED",
        reason_code="INSTANCE_GENERATED",
        metadata={
            "period_key": period_key,
            "expected_by": expected_by.isoformat(),
            "due_window_end": due_window_end.isoformat(),
        },
        now=stamp,
    )
    session.flush()
    return row, True


def _instance_for_update(
    session: Session,
    instance_id: str,
) -> ObligationInstanceRow | None:
    query = select(ObligationInstanceRow).where(
        ObligationInstanceRow.id == instance_id
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    return session.scalar(query)


def _definition_for_instance(
    session: Session,
    instance: ObligationInstanceRow,
) -> RecurringObligationDefinitionRow:
    row = session.get(RecurringObligationDefinitionRow, instance.definition_id)
    if row is None or row.tenant_id != instance.tenant_id:
        raise ObligationError("OBLIGATION_INSTANCE_DEFINITION_INVALID")
    return row


def _event_matches_definition(
    *,
    definition: RecurringObligationDefinitionRow,
    event: TimelineEventRow,
) -> bool:
    if event.actor_id != definition.expected_actor_key:
        return False
    if event.event_type != definition.expected_event_type:
        return False
    if definition.subject_type == "RESOURCE":
        return event.resource_id == definition.subject_id
    if definition.subject_type == "RELATIONSHIP":
        return event.relationship_id == definition.subject_id
    return False


def reconcile_obligation_event(
    session: Session,
    *,
    instance_id: str,
    timeline_event_id: str,
    fulfillment_fraction: float = 1.0,
    observed_value: dict | None = None,
    allow_shared_event: bool = False,
    now: datetime | None = None,
) -> tuple[ObligationFulfillmentRow, bool]:
    """Reconcile one observed TimelineEvent against one expectation instance."""
    if (
        isinstance(fulfillment_fraction, bool)
        or not isinstance(fulfillment_fraction, (int, float))
        or fulfillment_fraction <= 0
        or fulfillment_fraction > 1
    ):
        raise ObligationError("OBLIGATION_FULFILLMENT_FRACTION_INVALID")

    instance = _instance_for_update(session, instance_id)
    event = session.get(TimelineEventRow, timeline_event_id)
    if instance is None:
        raise ObligationError("OBLIGATION_INSTANCE_NOT_FOUND")
    if event is None:
        raise ObligationError("OBLIGATION_EVENT_NOT_FOUND")
    if instance.tenant_id != event.tenant_id:
        raise ObligationError("OBLIGATION_TENANT_MISMATCH")
    if event.visibility == "SECRET":
        raise ObligationError("OBLIGATION_SECRET_EVENT_NOT_RECONCILABLE")
    if instance.state in {"SATISFIED", "WAIVED", "SUPERSEDED"}:
        raise ObligationError("OBLIGATION_INSTANCE_TERMINAL")

    definition = _definition_for_instance(session, instance)
    if not _event_matches_definition(definition=definition, event=event):
        raise ObligationError("OBLIGATION_EVENT_DOES_NOT_MATCH")

    event_at = _utc(event.occurred_at)
    if event_at < _utc(instance.period_start) or event_at >= _utc(instance.period_end):
        raise ObligationError("OBLIGATION_EVENT_OUTSIDE_PERIOD")
    if event_at < _utc(definition.valid_from):
        raise ObligationError("OBLIGATION_EVENT_BEFORE_DEFINITION_VALIDITY")
    if (
        definition.valid_until is not None
        and event_at >= _utc(definition.valid_until)
    ):
        raise ObligationError("OBLIGATION_EVENT_AFTER_DEFINITION_VALIDITY")

    existing = session.scalar(
        select(ObligationFulfillmentRow).where(
            ObligationFulfillmentRow.instance_id == instance.id,
            ObligationFulfillmentRow.timeline_event_id == event.id,
        )
    )
    if existing is not None:
        return existing, False

    other_links = list(
        session.scalars(
            select(ObligationFulfillmentRow).where(
                ObligationFulfillmentRow.tenant_id == instance.tenant_id,
                ObligationFulfillmentRow.timeline_event_id == event.id,
                ObligationFulfillmentRow.instance_id != instance.id,
            )
        ).all()
    )
    if other_links and not allow_shared_event:
        raise ObligationError("OBLIGATION_EVENT_ALREADY_RECONCILED")

    effective_deadline = _utc(
        instance.extension_until
        if instance.extension_until is not None
        else instance.due_window_end
    )
    was_unconfirmed = instance.state == "UNCONFIRMED_AFTER_DUE"
    late_by_time = event_at > effective_deadline
    if other_links and allow_shared_event:
        reconciliation_kind = "EXPLICIT_SHARED"
    elif was_unconfirmed and not late_by_time:
        reconciliation_kind = "LATE_INGESTED"
    elif late_by_time:
        reconciliation_kind = "LATE_OBSERVED"
    else:
        reconciliation_kind = "ON_TIME"

    fraction = float(fulfillment_fraction)
    prior_ratio = float(instance.satisfaction_ratio)
    if prior_ratio + fraction > 1.0 + 1e-9:
        raise ObligationError("OBLIGATION_FULFILLMENT_OVERFLOW")
    new_ratio = min(1.0, prior_ratio + fraction)

    stamp = _utc(now)
    idempotency_key = (
        "obligation-fulfillment:"
        + stable_hash(
            {
                "instance_id": instance.id,
                "timeline_event_id": event.id,
            }
        )[:96]
    )
    fulfillment = ObligationFulfillmentRow(
        id=new_id(),
        tenant_id=instance.tenant_id,
        instance_id=instance.id,
        timeline_event_id=event.id,
        idempotency_key=idempotency_key,
        fulfillment_fraction=fraction,
        observed_value=observed_value or {},
        explicitly_shared=bool(other_links and allow_shared_event),
        reconciliation_kind=reconciliation_kind,
        created_at=stamp,
    )
    session.add(fulfillment)

    old_state = instance.state
    instance.satisfaction_ratio = new_ratio
    instance.updated_at = stamp

    if new_ratio >= 1.0:
        instance.state = "SATISFIED"
        instance.reconciliation_status = (
            "LATE_CONFIRMED"
            if was_unconfirmed or late_by_time
            else "CONFIRMED"
        )
        instance.uncertainty_code = None
        reason = (
            "LATE_EVIDENCE_CONFIRMED"
            if instance.reconciliation_status == "LATE_CONFIRMED"
            else "FULFILLMENT_CONFIRMED"
        )
    else:
        instance.state = "PARTIALLY_SATISFIED"
        instance.reconciliation_status = "PARTIAL"
        instance.uncertainty_code = "PARTIAL_CONFIRMING_EVIDENCE"
        reason = "PARTIAL_FULFILLMENT_EVIDENCE"

    _transition(
        session,
        instance=instance,
        from_state=old_state,
        to_state=instance.state,
        reason_code=reason,
        evidence_ref=event.id,
        metadata={
            "fulfillment_fraction": fraction,
            "satisfaction_ratio": new_ratio,
            "reconciliation_kind": reconciliation_kind,
        },
        now=stamp,
    )
    session.flush()
    return fulfillment, True


def extend_obligation_instance(
    session: Session,
    *,
    instance_id: str,
    extension_until: datetime,
    decision_actor_key: str,
    decision_ref: str,
    now: datetime | None = None,
) -> ObligationInstanceRow:
    instance = _instance_for_update(session, instance_id)
    if instance is None:
        raise ObligationError("OBLIGATION_INSTANCE_NOT_FOUND")
    if instance.state in {"SATISFIED", "WAIVED", "SUPERSEDED"}:
        raise ObligationError("OBLIGATION_INSTANCE_NOT_EXTENDABLE")

    owner = _require_owner(
        session,
        tenant_id=instance.tenant_id,
        actor_key=decision_actor_key,
    )
    reference = _bounded(
        decision_ref,
        code="OBLIGATION_DECISION_REF",
        limit=240,
    )
    extended = _utc(extension_until)
    current_deadline = _utc(
        instance.extension_until
        if instance.extension_until is not None
        else instance.due_window_end
    )
    if extended <= current_deadline or extended >= _utc(instance.period_end):
        raise ObligationError("OBLIGATION_EXTENSION_INVALID")

    stamp = _utc(now)
    old_state = instance.state
    instance.state = "EXTENDED"
    instance.reconciliation_status = "EXTENDED"
    instance.uncertainty_code = "OWNER_EXTENSION_ACTIVE"
    instance.extension_until = extended
    instance.updated_at = stamp
    _transition(
        session,
        instance=instance,
        from_state=old_state,
        to_state="EXTENDED",
        reason_code="OWNER_EXTENDED_DUE_WINDOW",
        decision_actor_key=owner,
        decision_ref=reference,
        metadata={"extension_until": extended.isoformat()},
        now=stamp,
    )
    session.flush()
    return instance


def waive_obligation_instance(
    session: Session,
    *,
    instance_id: str,
    decision_actor_key: str,
    decision_ref: str,
    now: datetime | None = None,
) -> ObligationInstanceRow:
    instance = _instance_for_update(session, instance_id)
    if instance is None:
        raise ObligationError("OBLIGATION_INSTANCE_NOT_FOUND")
    if instance.state == "WAIVED":
        return instance
    if instance.state in {"SATISFIED", "SUPERSEDED"}:
        raise ObligationError("OBLIGATION_INSTANCE_NOT_WAIVABLE")

    owner = _require_owner(
        session,
        tenant_id=instance.tenant_id,
        actor_key=decision_actor_key,
    )
    reference = _bounded(
        decision_ref,
        code="OBLIGATION_DECISION_REF",
        limit=240,
    )
    stamp = _utc(now)
    old_state = instance.state
    instance.state = "WAIVED"
    instance.reconciliation_status = "WAIVED"
    instance.uncertainty_code = None
    instance.waived_at = stamp
    instance.updated_at = stamp
    _transition(
        session,
        instance=instance,
        from_state=old_state,
        to_state="WAIVED",
        reason_code="OWNER_WAIVED_OBLIGATION",
        decision_actor_key=owner,
        decision_ref=reference,
        now=stamp,
    )
    session.flush()
    return instance


def advance_due_obligation_states(
    session: Session,
    *,
    tenant_id: str,
    now: datetime | None = None,
) -> int:
    """Mark overdue expectations as unconfirmed, never as factual non-payment."""
    stamp = _utc(now)
    query = select(ObligationInstanceRow).where(
        ObligationInstanceRow.tenant_id == tenant_id,
        ObligationInstanceRow.state.in_(
            {"EXPECTED", "PARTIALLY_SATISFIED", "EXTENDED"}
        ),
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    rows = list(session.scalars(query).all())
    changed = 0
    for row in rows:
        deadline = _utc(
            row.extension_until
            if row.extension_until is not None
            else row.due_window_end
        )
        if deadline >= stamp:
            continue
        if row.satisfaction_ratio > 0:
            old_state = row.state
            previous_uncertainty = row.uncertainty_code
            row.state = "PARTIALLY_SATISFIED"
            row.reconciliation_status = "PARTIAL"
            row.uncertainty_code = "PARTIAL_EVIDENCE_AFTER_DUE"
            row.updated_at = stamp
            if (
                old_state != row.state
                or previous_uncertainty != row.uncertainty_code
            ):
                _transition(
                    session,
                    instance=row,
                    from_state=old_state,
                    to_state="PARTIALLY_SATISFIED",
                    reason_code="PARTIAL_EVIDENCE_REMAINS_AFTER_DUE",
                    metadata={
                        "absence_is_fact": False,
                        "satisfaction_ratio": row.satisfaction_ratio,
                        "deadline": deadline.isoformat(),
                    },
                    now=stamp,
                )
                changed += 1
            continue

        old_state = row.state
        row.state = "UNCONFIRMED_AFTER_DUE"
        row.reconciliation_status = "UNCONFIRMED"
        row.uncertainty_code = "NO_CONFIRMING_EVIDENCE_AFTER_DUE"
        row.updated_at = stamp
        _transition(
            session,
            instance=row,
            from_state=old_state,
            to_state="UNCONFIRMED_AFTER_DUE",
            reason_code="DUE_WINDOW_CLOSED_WITHOUT_CONFIRMING_EVIDENCE",
            metadata={
                "absence_is_fact": False,
                "asserts_non_payment": False,
                "deadline": deadline.isoformat(),
            },
            now=stamp,
        )
        changed += 1

    if changed:
        session.flush()
    return changed


__all__ = [
    "ObligationError",
    "advance_due_obligation_states",
    "create_recurring_obligation_definition",
    "extend_obligation_instance",
    "generate_monthly_obligation_instance",
    "reconcile_obligation_event",
    "waive_obligation_instance",
]
