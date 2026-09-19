from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
    PendingIntentRow,
)
from attention_router.infrastructure.repository import audit


PENDING_INTENT_SCHEMA_VERSION = "1"
PENDING_INTENT_STATES = frozenset(
    {"PENDING", "RESOLVED", "CANCELED", "SUPERSEDED", "EXPIRED"}
)
TERMINAL_PENDING_INTENT_STATES = PENDING_INTENT_STATES - {"PENDING"}
CANDIDATE_CONFIDENCE = frozenset({"high", "medium", "low"})
CAPABILITY_MAPPING_STATUSES = frozenset(
    {"AVAILABLE", "UNAVAILABLE", "NOT_APPLICABLE"}
)


class PendingIntentError(RuntimeError):
    pass


class PendingIntentConflict(PendingIntentError):
    pass


class PendingIntentScopeError(PendingIntentError):
    pass


class PendingIntentExpired(PendingIntentError):
    pass


class PendingIntentCandidateError(PendingIntentError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalized_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise PendingIntentCandidateError("PENDING_INTENT_CANDIDATE_INVALID")
    allowed = {
        "candidate_key",
        "semantic_intent_key",
        "parameters",
        "confidence",
        "evidence_summary",
        "capability_mapping",
    }
    if set(candidate) - allowed:
        raise PendingIntentCandidateError("PENDING_INTENT_CANDIDATE_EXTRA_FIELDS")

    candidate_key = candidate.get("candidate_key")
    semantic_intent_key = candidate.get("semantic_intent_key")
    parameters = candidate.get("parameters")
    confidence = candidate.get("confidence")
    evidence_summary = candidate.get("evidence_summary")
    mapping = candidate.get("capability_mapping")

    if not isinstance(candidate_key, str) or not candidate_key.strip() or len(candidate_key) > 64:
        raise PendingIntentCandidateError("PENDING_INTENT_CANDIDATE_KEY_INVALID")
    if (
        not isinstance(semantic_intent_key, str)
        or not semantic_intent_key.strip()
        or len(semantic_intent_key) > 120
    ):
        raise PendingIntentCandidateError("PENDING_INTENT_SEMANTIC_INTENT_KEY_INVALID")
    if not isinstance(parameters, dict):
        raise PendingIntentCandidateError("PENDING_INTENT_PARAMETERS_INVALID")
    if confidence not in CANDIDATE_CONFIDENCE:
        raise PendingIntentCandidateError("PENDING_INTENT_CONFIDENCE_INVALID")
    if evidence_summary is not None and (
        not isinstance(evidence_summary, str) or len(evidence_summary) > 320
    ):
        raise PendingIntentCandidateError("PENDING_INTENT_EVIDENCE_SUMMARY_INVALID")
    if not isinstance(mapping, dict):
        raise PendingIntentCandidateError("PENDING_INTENT_CAPABILITY_MAPPING_INVALID")
    if set(mapping) - {"status", "capability_key"}:
        raise PendingIntentCandidateError("PENDING_INTENT_CAPABILITY_MAPPING_EXTRA_FIELDS")

    mapping_status = mapping.get("status")
    capability_key = mapping.get("capability_key")
    if mapping_status not in CAPABILITY_MAPPING_STATUSES:
        raise PendingIntentCandidateError("PENDING_INTENT_CAPABILITY_STATUS_INVALID")
    if mapping_status == "AVAILABLE":
        if (
            not isinstance(capability_key, str)
            or not capability_key.strip()
            or len(capability_key) > 160
        ):
            raise PendingIntentCandidateError("PENDING_INTENT_CAPABILITY_KEY_REQUIRED")
    elif capability_key is not None:
        raise PendingIntentCandidateError("PENDING_INTENT_CAPABILITY_KEY_NOT_ALLOWED")

    return {
        "candidate_key": candidate_key.strip(),
        "semantic_intent_key": semantic_intent_key.strip(),
        "parameters": parameters,
        "confidence": confidence,
        "evidence_summary": evidence_summary,
        "capability_mapping": {
            "status": mapping_status,
            "capability_key": capability_key.strip() if isinstance(capability_key, str) else None,
        },
    }


def build_candidate_set(
    candidates: list[dict[str, Any]],
    *,
    semantic_registry_version: str,
) -> dict[str, Any]:
    if not isinstance(semantic_registry_version, str) or not semantic_registry_version.strip():
        raise PendingIntentCandidateError("PENDING_INTENT_REGISTRY_VERSION_INVALID")
    if not isinstance(candidates, list) or not candidates or len(candidates) > 8:
        raise PendingIntentCandidateError("PENDING_INTENT_CANDIDATE_COUNT_INVALID")

    normalized = [_normalized_candidate(candidate) for candidate in candidates]
    keys = [candidate["candidate_key"] for candidate in normalized]
    if len(keys) != len(set(keys)):
        raise PendingIntentCandidateError("PENDING_INTENT_CANDIDATE_KEY_DUPLICATE")

    return {
        "schema_version": PENDING_INTENT_SCHEMA_VERSION,
        "semantic_registry_version": semantic_registry_version.strip(),
        "candidates": normalized,
    }


def candidate_set_fingerprint(candidate_set: dict[str, Any]) -> str:
    return stable_hash(candidate_set)


def _candidate_keys(row: PendingIntentRow) -> set[str]:
    candidates = (row.candidate_set or {}).get("candidates")
    if not isinstance(candidates, list):
        raise PendingIntentCandidateError("PENDING_INTENT_STORED_CANDIDATES_INVALID")
    return {
        candidate.get("candidate_key")
        for candidate in candidates
        if isinstance(candidate, dict) and isinstance(candidate.get("candidate_key"), str)
    }


def _lock_row(session: Session, intent_id: str) -> PendingIntentRow | None:
    query = select(PendingIntentRow).where(PendingIntentRow.id == intent_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    return session.scalar(query)


def _scope_matches(
    row: PendingIntentRow,
    *,
    tenant_id: str,
    represented_owner_actor_key: str,
    source_channel: str,
    conversation_key_hash: str,
) -> bool:
    return (
        row.tenant_id == tenant_id
        and row.represented_owner_actor_key == represented_owner_actor_key
        and row.source_channel == source_channel
        and row.conversation_key_hash == conversation_key_hash
    )


def _validate_source(
    session: Session,
    *,
    tenant_id: str,
    source_inbound_event_id: str,
    source_interaction_id: str,
) -> tuple[InboundEventRow, InteractionRow]:
    event = session.get(InboundEventRow, source_inbound_event_id)
    interaction = session.get(InteractionRow, source_interaction_id)
    if event is None or interaction is None:
        raise PendingIntentScopeError("PENDING_INTENT_SOURCE_NOT_FOUND")
    if event.tenant_id != tenant_id or interaction.tenant_id != tenant_id:
        raise PendingIntentScopeError("PENDING_INTENT_TENANT_SCOPE_MISMATCH")
    if event.interaction_id != interaction.id:
        raise PendingIntentScopeError("PENDING_INTENT_INTERACTION_SCOPE_MISMATCH")
    return event, interaction


def _expire_row(
    session: Session,
    row: PendingIntentRow,
    *,
    timestamp: datetime,
) -> None:
    if row.state != "PENDING":
        return
    row.state = "EXPIRED"
    row.version += 1
    row.updated_at = timestamp
    audit(
        session,
        row.source_interaction_id,
        "intent_clarification.expired",
        {"pending_intent_id": row.id},
        row.correlation_id,
        row.source_inbound_event_id,
        previous_state="PENDING",
        next_state="EXPIRED",
        origin="intent_clarification",
        tenant_id=row.tenant_id,
        created_at=timestamp,
    )


def expire_due_pending_intents(
    session: Session,
    *,
    timestamp: datetime | None = None,
    limit: int = 100,
) -> int:
    if limit < 1 or limit > 1000:
        raise ValueError("PENDING_INTENT_EXPIRE_LIMIT_OUT_OF_RANGE")
    stamp = _utc(timestamp or now_utc())
    query = (
        select(PendingIntentRow)
        .where(
            PendingIntentRow.state == "PENDING",
            PendingIntentRow.expires_at <= stamp,
        )
        .order_by(PendingIntentRow.expires_at, PendingIntentRow.id)
        .limit(limit)
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    rows = list(session.scalars(query).all())
    for row in rows:
        _expire_row(session, row, timestamp=stamp)
    session.flush()
    return len(rows)


def find_active_pending_intent(
    session: Session,
    *,
    tenant_id: str,
    represented_owner_actor_key: str,
    source_channel: str,
    conversation_key_hash: str,
    timestamp: datetime | None = None,
) -> PendingIntentRow | None:
    stamp = _utc(timestamp or now_utc())
    return session.scalar(
        select(PendingIntentRow).where(
            PendingIntentRow.tenant_id == tenant_id,
            PendingIntentRow.represented_owner_actor_key == represented_owner_actor_key,
            PendingIntentRow.source_channel == source_channel,
            PendingIntentRow.conversation_key_hash == conversation_key_hash,
            PendingIntentRow.state == "PENDING",
            PendingIntentRow.expires_at > stamp,
        )
    )


def create_pending_intent(
    session: Session,
    *,
    tenant_id: str,
    represented_owner_actor_key: str,
    source_inbound_event_id: str,
    source_interaction_id: str,
    source_channel: str,
    conversation_key_hash: str,
    semantic_registry_version: str,
    ambiguity_reason: str,
    candidates: list[dict[str, Any]],
    expires_at: datetime,
    provenance: dict[str, Any] | None = None,
    timestamp: datetime | None = None,
) -> PendingIntentRow:
    stamp = _utc(timestamp or now_utc())
    expiry = _utc(expires_at)
    if expiry <= stamp:
        raise PendingIntentExpired("PENDING_INTENT_EXPIRY_NOT_FUTURE")
    if not represented_owner_actor_key or not source_channel or not conversation_key_hash:
        raise PendingIntentScopeError("PENDING_INTENT_SCOPE_INVALID")
    if not ambiguity_reason or len(ambiguity_reason) > 120:
        raise PendingIntentError("PENDING_INTENT_AMBIGUITY_REASON_INVALID")

    event, _interaction = _validate_source(
        session,
        tenant_id=tenant_id,
        source_inbound_event_id=source_inbound_event_id,
        source_interaction_id=source_interaction_id,
    )
    candidate_set = build_candidate_set(
        candidates,
        semantic_registry_version=semantic_registry_version,
    )
    fingerprint = candidate_set_fingerprint(candidate_set)

    existing = session.scalar(
        select(PendingIntentRow).where(
            PendingIntentRow.source_inbound_event_id == source_inbound_event_id
        )
    )
    if existing is not None:
        if (
            _scope_matches(
                existing,
                tenant_id=tenant_id,
                represented_owner_actor_key=represented_owner_actor_key,
                source_channel=source_channel,
                conversation_key_hash=conversation_key_hash,
            )
            and existing.source_interaction_id == source_interaction_id
            and existing.candidate_set_fingerprint == fingerprint
            and existing.semantic_registry_version == semantic_registry_version
        ):
            return existing
        raise PendingIntentConflict("PENDING_INTENT_SOURCE_REPLAY_CONFLICT")

    due = list(
        session.scalars(
            select(PendingIntentRow).where(
                PendingIntentRow.tenant_id == tenant_id,
                PendingIntentRow.represented_owner_actor_key == represented_owner_actor_key,
                PendingIntentRow.source_channel == source_channel,
                PendingIntentRow.conversation_key_hash == conversation_key_hash,
                PendingIntentRow.state == "PENDING",
                PendingIntentRow.expires_at <= stamp,
            )
        ).all()
    )
    for row in due:
        _expire_row(session, row, timestamp=stamp)

    active = find_active_pending_intent(
        session,
        tenant_id=tenant_id,
        represented_owner_actor_key=represented_owner_actor_key,
        source_channel=source_channel,
        conversation_key_hash=conversation_key_hash,
        timestamp=stamp,
    )
    if active is not None:
        raise PendingIntentConflict("PENDING_INTENT_ACTIVE_SCOPE_CONFLICT")

    row = PendingIntentRow(
        id=new_id(),
        tenant_id=tenant_id,
        represented_owner_actor_key=represented_owner_actor_key,
        source_inbound_event_id=source_inbound_event_id,
        source_interaction_id=source_interaction_id,
        source_channel=source_channel,
        conversation_key_hash=conversation_key_hash,
        semantic_registry_version=semantic_registry_version,
        state="PENDING",
        ambiguity_reason=ambiguity_reason,
        candidate_set=candidate_set,
        candidate_set_fingerprint=fingerprint,
        expires_at=expiry,
        correlation_id=event.correlation_id,
        provenance=provenance or {},
        version=1,
        created_at=stamp,
        updated_at=stamp,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        replay = session.scalar(
            select(PendingIntentRow).where(
                PendingIntentRow.source_inbound_event_id == source_inbound_event_id
            )
        )
        if replay is not None and replay.candidate_set_fingerprint == fingerprint:
            return replay
        raise PendingIntentConflict("PENDING_INTENT_PERSISTENCE_CONFLICT") from exc

    audit(
        session,
        source_interaction_id,
        "intent_clarification.created",
        {
            "pending_intent_id": row.id,
            "candidate_count": len(candidate_set["candidates"]),
            "candidate_set_fingerprint": fingerprint,
            "semantic_registry_version": semantic_registry_version,
            "ambiguity_reason": ambiguity_reason,
        },
        row.correlation_id,
        source_inbound_event_id,
        previous_state=None,
        next_state="PENDING",
        origin="intent_clarification",
        tenant_id=tenant_id,
        created_at=stamp,
    )
    session.flush()
    return row


def attach_clarification_outbox(
    session: Session,
    *,
    pending_intent_id: str,
    outbox_id: str,
    timestamp: datetime | None = None,
) -> PendingIntentRow:
    stamp = _utc(timestamp or now_utc())
    row = _lock_row(session, pending_intent_id)
    if row is None:
        raise PendingIntentError("PENDING_INTENT_NOT_FOUND")
    if row.state != "PENDING":
        raise PendingIntentConflict("PENDING_INTENT_NOT_PENDING")
    if row.expires_at <= stamp:
        _expire_row(session, row, timestamp=stamp)
        session.flush()
        raise PendingIntentExpired("PENDING_INTENT_EXPIRED")

    outbox = session.get(OutboxMessageRow, outbox_id)
    if outbox is None or outbox.interaction_id != row.source_interaction_id:
        raise PendingIntentScopeError("PENDING_INTENT_OUTBOX_SCOPE_MISMATCH")
    if row.clarification_outbox_id is not None:
        if row.clarification_outbox_id == outbox_id:
            return row
        raise PendingIntentConflict("PENDING_INTENT_CLARIFICATION_ALREADY_ATTACHED")
    linked = session.scalar(
        select(PendingIntentRow).where(
            PendingIntentRow.clarification_outbox_id == outbox_id,
            PendingIntentRow.id != row.id,
        )
    )
    if linked is not None:
        raise PendingIntentConflict("PENDING_INTENT_OUTBOX_ALREADY_LINKED")

    row.clarification_outbox_id = outbox_id
    row.version += 1
    row.updated_at = stamp
    audit(
        session,
        row.source_interaction_id,
        "intent_clarification.prompt_enqueued",
        {"pending_intent_id": row.id, "outbox_id": outbox_id},
        row.correlation_id,
        row.source_inbound_event_id,
        origin="intent_clarification",
        tenant_id=row.tenant_id,
        created_at=stamp,
    )
    session.flush()
    return row


def mark_clarification_delivered(
    session: Session,
    *,
    pending_intent_id: str,
    outbox_id: str,
    timestamp: datetime | None = None,
) -> PendingIntentRow:
    stamp = _utc(timestamp or now_utc())
    row = _lock_row(session, pending_intent_id)
    if row is None:
        raise PendingIntentError("PENDING_INTENT_NOT_FOUND")
    if row.clarification_outbox_id != outbox_id:
        raise PendingIntentScopeError("PENDING_INTENT_OUTBOX_SCOPE_MISMATCH")
    if row.clarification_delivered_at is not None:
        return row
    row.clarification_delivered_at = stamp
    row.version += 1
    row.updated_at = stamp
    audit(
        session,
        row.source_interaction_id,
        "intent_clarification.prompt_delivered",
        {"pending_intent_id": row.id, "outbox_id": outbox_id},
        row.correlation_id,
        row.source_inbound_event_id,
        origin="intent_clarification",
        tenant_id=row.tenant_id,
        created_at=stamp,
    )
    session.flush()
    return row


def _validate_resolution_scope(
    session: Session,
    row: PendingIntentRow,
    *,
    tenant_id: str,
    represented_owner_actor_key: str,
    source_channel: str,
    conversation_key_hash: str,
    resolution_inbound_event_id: str,
) -> InboundEventRow:
    if not _scope_matches(
        row,
        tenant_id=tenant_id,
        represented_owner_actor_key=represented_owner_actor_key,
        source_channel=source_channel,
        conversation_key_hash=conversation_key_hash,
    ):
        raise PendingIntentScopeError("PENDING_INTENT_RESOLUTION_SCOPE_MISMATCH")
    event = session.get(InboundEventRow, resolution_inbound_event_id)
    if event is None or event.tenant_id != tenant_id:
        raise PendingIntentScopeError("PENDING_INTENT_RESOLUTION_EVENT_SCOPE_MISMATCH")
    if event.id == row.source_inbound_event_id:
        raise PendingIntentScopeError("PENDING_INTENT_SOURCE_CANNOT_RESOLVE_ITSELF")
    if _utc(event.received_at) < _utc(row.created_at):
        raise PendingIntentScopeError("PENDING_INTENT_RESOLUTION_PRECEDES_CLARIFICATION")
    other = session.scalar(
        select(PendingIntentRow).where(
            PendingIntentRow.resolution_inbound_event_id == resolution_inbound_event_id,
            PendingIntentRow.id != row.id,
        )
    )
    if other is not None:
        raise PendingIntentConflict("PENDING_INTENT_RESOLUTION_EVENT_ALREADY_USED")
    return event


def resolve_pending_intent(
    session: Session,
    *,
    pending_intent_id: str,
    tenant_id: str,
    represented_owner_actor_key: str,
    source_channel: str,
    conversation_key_hash: str,
    resolution_inbound_event_id: str,
    selected_candidate_key: str,
    resolution_kind: str,
    timestamp: datetime | None = None,
) -> PendingIntentRow:
    stamp = _utc(timestamp or now_utc())
    row = _lock_row(session, pending_intent_id)
    if row is None:
        raise PendingIntentError("PENDING_INTENT_NOT_FOUND")

    if row.state == "RESOLVED":
        if (
            row.resolution_inbound_event_id == resolution_inbound_event_id
            and row.selected_candidate_key == selected_candidate_key
            and row.resolution_kind == resolution_kind
        ):
            return row
        raise PendingIntentConflict("PENDING_INTENT_ALREADY_RESOLVED")
    if row.state != "PENDING":
        raise PendingIntentConflict("PENDING_INTENT_TERMINAL")
    if row.expires_at <= stamp:
        _expire_row(session, row, timestamp=stamp)
        session.flush()
        raise PendingIntentExpired("PENDING_INTENT_EXPIRED")
    if selected_candidate_key not in _candidate_keys(row):
        raise PendingIntentCandidateError("PENDING_INTENT_CANDIDATE_NOT_FOUND")
    if not resolution_kind or len(resolution_kind) > 40:
        raise PendingIntentError("PENDING_INTENT_RESOLUTION_KIND_INVALID")

    _validate_resolution_scope(
        session,
        row,
        tenant_id=tenant_id,
        represented_owner_actor_key=represented_owner_actor_key,
        source_channel=source_channel,
        conversation_key_hash=conversation_key_hash,
        resolution_inbound_event_id=resolution_inbound_event_id,
    )
    previous = row.state
    row.state = "RESOLVED"
    row.resolution_inbound_event_id = resolution_inbound_event_id
    row.selected_candidate_key = selected_candidate_key
    row.resolution_kind = resolution_kind
    row.resolved_at = stamp
    row.version += 1
    row.updated_at = stamp
    audit(
        session,
        row.source_interaction_id,
        "intent_clarification.resolved",
        {
            "pending_intent_id": row.id,
            "selected_candidate_key": selected_candidate_key,
            "candidate_set_fingerprint": row.candidate_set_fingerprint,
            "resolution_kind": resolution_kind,
        },
        row.correlation_id,
        resolution_inbound_event_id,
        previous_state=previous,
        next_state="RESOLVED",
        origin="intent_clarification",
        tenant_id=row.tenant_id,
        created_at=stamp,
    )
    session.flush()
    return row


def cancel_pending_intent(
    session: Session,
    *,
    pending_intent_id: str,
    reason: str,
    resolution_inbound_event_id: str | None = None,
    timestamp: datetime | None = None,
) -> PendingIntentRow:
    stamp = _utc(timestamp or now_utc())
    row = _lock_row(session, pending_intent_id)
    if row is None:
        raise PendingIntentError("PENDING_INTENT_NOT_FOUND")
    if row.state == "CANCELED":
        return row
    if row.state != "PENDING":
        raise PendingIntentConflict("PENDING_INTENT_TERMINAL")
    if not reason or len(reason) > 120:
        raise PendingIntentError("PENDING_INTENT_CANCEL_REASON_INVALID")
    if resolution_inbound_event_id is not None:
        event = session.get(InboundEventRow, resolution_inbound_event_id)
        if event is None or event.tenant_id != row.tenant_id:
            raise PendingIntentScopeError("PENDING_INTENT_RESOLUTION_EVENT_SCOPE_MISMATCH")
        if event.id == row.source_inbound_event_id:
            raise PendingIntentScopeError("PENDING_INTENT_SOURCE_CANNOT_RESOLVE_ITSELF")
        row.resolution_inbound_event_id = event.id
    row.state = "CANCELED"
    row.resolution_kind = reason
    row.version += 1
    row.updated_at = stamp
    audit(
        session,
        row.source_interaction_id,
        "intent_clarification.canceled",
        {"pending_intent_id": row.id, "reason": reason},
        row.correlation_id,
        resolution_inbound_event_id or row.source_inbound_event_id,
        previous_state="PENDING",
        next_state="CANCELED",
        origin="intent_clarification",
        tenant_id=row.tenant_id,
        created_at=stamp,
    )
    session.flush()
    return row


def supersede_pending_intent(
    session: Session,
    *,
    pending_intent_id: str,
    superseding_source_inbound_event_id: str,
    reason: str = "NEWER_INCOMPATIBLE_CLARIFICATION",
    timestamp: datetime | None = None,
) -> PendingIntentRow:
    stamp = _utc(timestamp or now_utc())
    row = _lock_row(session, pending_intent_id)
    if row is None:
        raise PendingIntentError("PENDING_INTENT_NOT_FOUND")
    if row.state == "SUPERSEDED":
        return row
    if row.state != "PENDING":
        raise PendingIntentConflict("PENDING_INTENT_TERMINAL")
    event = session.get(InboundEventRow, superseding_source_inbound_event_id)
    if event is None or event.tenant_id != row.tenant_id:
        raise PendingIntentScopeError("PENDING_INTENT_SUPERSEDING_EVENT_SCOPE_MISMATCH")
    row.state = "SUPERSEDED"
    row.resolution_kind = reason[:40]
    row.provenance = {
        **(row.provenance or {}),
        "superseding_source_inbound_event_id": superseding_source_inbound_event_id,
    }
    row.version += 1
    row.updated_at = stamp
    audit(
        session,
        row.source_interaction_id,
        "intent_clarification.superseded",
        {
            "pending_intent_id": row.id,
            "reason": reason,
            "superseding_source_event_id": superseding_source_inbound_event_id,
        },
        row.correlation_id,
        superseding_source_inbound_event_id,
        previous_state="PENDING",
        next_state="SUPERSEDED",
        origin="intent_clarification",
        tenant_id=row.tenant_id,
        created_at=stamp,
    )
    session.flush()
    return row


__all__ = [
    "CAPABILITY_MAPPING_STATUSES",
    "CANDIDATE_CONFIDENCE",
    "PENDING_INTENT_SCHEMA_VERSION",
    "PendingIntentCandidateError",
    "PendingIntentConflict",
    "PendingIntentError",
    "PendingIntentExpired",
    "PendingIntentScopeError",
    "attach_clarification_outbox",
    "build_candidate_set",
    "cancel_pending_intent",
    "candidate_set_fingerprint",
    "create_pending_intent",
    "expire_due_pending_intents",
    "find_active_pending_intent",
    "mark_clarification_delivered",
    "resolve_pending_intent",
    "supersede_pending_intent",
]
