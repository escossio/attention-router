from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.personal_context_anomaly_suggestions import (
    SUGGESTION_CLAIM_PREDICATE,
    SUGGESTION_SOURCE_QUALITY,
)
from attention_router.application.personal_context_runtime import (
    PERSONAL_CONTEXT_SUGGESTION_OUTBOX_ACTION,
)
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    ActorBindingRow,
    InboundEventRow,
    MemoryActorRow,
    MemoryClaimRow,
    OutboxMessageRow,
)


_INTERESTED = re.compile(
    r"^(?:quero revisar|quero ver o contexto|me mostre o contexto)$"
)
_DISMISS = re.compile(
    r"^(?:não quero revisar|nao quero revisar|ignorar sugestão|ignorar sugestao)$"
)


class SuggestionDecision(StrEnum):
    INTERESTED = "INTERESTED"
    DISMISS = "DISMISS"


@dataclass(frozen=True, slots=True)
class SuggestionReplyResolution:
    suggestion_id: str
    decision: SuggestionDecision
    lifecycle_claim_id: str | None
    status: str
    reason_code: str | None = None


class SuggestionReplyError(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_suggestion_reply(text: str) -> SuggestionDecision | None:
    normalized = " ".join(text.strip().lower().split())
    if _INTERESTED.fullmatch(normalized):
        return SuggestionDecision.INTERESTED
    if _DISMISS.fullmatch(normalized):
        return SuggestionDecision.DISMISS
    return None


def _active_proposals(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    now: datetime,
) -> tuple[MemoryClaimRow, ...]:
    actor_ids = session.scalars(
        select(MemoryActorRow.id).where(
            MemoryActorRow.tenant_id == tenant_id,
            MemoryActorRow.actor_key == actor_key,
        )
    ).all()
    if not actor_ids:
        return ()

    rows = session.scalars(
        select(MemoryClaimRow)
        .where(
            MemoryClaimRow.subject_actor_id.in_(actor_ids),
            MemoryClaimRow.predicate == SUGGESTION_CLAIM_PREDICATE,
            MemoryClaimRow.source_quality == SUGGESTION_SOURCE_QUALITY,
            MemoryClaimRow.status == "ACTIVE",
        )
        .order_by(MemoryClaimRow.updated_at.desc(), MemoryClaimRow.id.desc())
    ).all()
    return tuple(
        row
        for row in rows
        if (row.object_json or {}).get("lifecycle_state") == "PROPOSED"
        and row.valid_until is not None
        and _utc(row.valid_until) > now
    )


def _delivered_proposal(
    session: Session,
    *,
    suggestion_claim: MemoryClaimRow,
    external_actor_id: str,
    received_at: datetime,
) -> OutboxMessageRow | None:
    suggestion_id = (suggestion_claim.context or {}).get("suggestion_id")
    if not isinstance(suggestion_id, str):
        return None

    rows = session.scalars(
        select(OutboxMessageRow)
        .where(
            OutboxMessageRow.action_type
            == PERSONAL_CONTEXT_SUGGESTION_OUTBOX_ACTION,
            OutboxMessageRow.status == "DONE",
            OutboxMessageRow.completed_at.is_not(None),
        )
        .order_by(
            OutboxMessageRow.completed_at.desc(),
            OutboxMessageRow.id.desc(),
        )
    ).all()
    matching = [
        row
        for row in rows
        if row.payload.get("suggestion_id") == suggestion_id
        and row.payload.get("suggestion_claim_id") == suggestion_claim.id
        and row.payload.get("external_actor_id") == external_actor_id
        and _utc(row.completed_at) <= received_at
    ]
    if len(matching) != 1:
        return None
    return matching[0]


def _owner_event_is_valid(
    session: Session,
    *,
    receipt: InboundEventRow,
    actor_key: str,
) -> bool:
    payload = receipt.payload or {}
    metadata = payload.get("metadata") or {}
    external_actor_id = payload.get("actor_id")
    if (
        payload.get("event_origin") != "OWNER_COMMAND"
        or payload.get("owner_authenticated") is not True
        or metadata.get("from_me") is not True
        or metadata.get("owner_self_chat") is not True
        or metadata.get("from_me_classification") != "OWNER_COMMAND"
        or metadata.get("final_from_me_classification") != "OWNER_COMMAND"
        or not isinstance(external_actor_id, str)
        or not external_actor_id
    ):
        return False
    binding = session.scalar(
        select(ActorBindingRow).where(
            ActorBindingRow.tenant_id == receipt.tenant_id,
            ActorBindingRow.actor_key == actor_key,
            ActorBindingRow.source == receipt.source,
            ActorBindingRow.external_actor_id == external_actor_id,
            ActorBindingRow.is_active.is_(True),
        )
    )
    return binding is not None


def _source_chain_current(
    session: Session,
    *,
    suggestion_claim: MemoryClaimRow,
    now: datetime,
) -> bool:
    context = suggestion_claim.context or {}
    anomaly = session.get(
        MemoryClaimRow,
        context.get("source_anomaly_claim_id"),
    )
    sequence = session.get(
        MemoryClaimRow,
        context.get("source_sequence_claim_id"),
    )
    return (
        anomaly is not None
        and anomaly.status == "ACTIVE"
        and anomaly.predicate == "context.pattern.sequence_anomaly"
        and anomaly.source_quality == "DERIVED_PATTERN"
        and (
            anomaly.valid_until is None
            or _utc(anomaly.valid_until) > now
        )
        and sequence is not None
        and sequence.status == "ACTIVE"
        and sequence.predicate == "context.pattern.event_sequence"
        and sequence.source_quality == "DERIVED_PATTERN"
        and (
            sequence.valid_until is None
            or _utc(sequence.valid_until) > now
        )
    )


def _event_already_consumed(
    session: Session,
    *,
    actor_id: str,
    event_id: str,
) -> bool:
    rows = session.scalars(
        select(MemoryClaimRow).where(
            MemoryClaimRow.subject_actor_id == actor_id,
            MemoryClaimRow.predicate == SUGGESTION_CLAIM_PREDICATE,
            MemoryClaimRow.source_quality == SUGGESTION_SOURCE_QUALITY,
        )
    ).all()
    return any(
        (row.context or {}).get("resolution_inbound_event_id") == event_id
        for row in rows
    )


def resolve_explicit_suggestion_reply(
    session: Session,
    *,
    receipt: InboundEventRow,
    actor_key: str,
    text: str,
) -> SuggestionReplyResolution | None:
    """Resolve one delivered proposal-only suggestion without execution semantics."""

    decision = parse_suggestion_reply(text)
    if decision is None:
        return None
    if not _owner_event_is_valid(
        session,
        receipt=receipt,
        actor_key=actor_key,
    ):
        return None

    payload = receipt.payload or {}
    external_actor_id = payload["actor_id"]
    received_at = _utc(receipt.received_at)
    candidates: list[MemoryClaimRow] = []
    for proposal in _active_proposals(
        session,
        tenant_id=receipt.tenant_id,
        actor_key=actor_key,
        now=received_at,
    ):
        if _delivered_proposal(
            session,
            suggestion_claim=proposal,
            external_actor_id=external_actor_id,
            received_at=received_at,
        ) is not None:
            candidates.append(proposal)

    if not candidates:
        return None
    if len(candidates) != 1:
        raise SuggestionReplyError("SUGGESTION_REPLY_AMBIGUOUS")

    current = candidates[0]
    suggestion_id = (current.context or {}).get("suggestion_id")
    if not isinstance(suggestion_id, str):
        raise SuggestionReplyError("SUGGESTION_ID_MISSING")

    if not _source_chain_current(
        session,
        suggestion_claim=current,
        now=received_at,
    ):
        return SuggestionReplyResolution(
            suggestion_id=suggestion_id,
            decision=decision,
            lifecycle_claim_id=None,
            status="SOURCE_INVALIDATED",
            reason_code="SUGGESTION_SOURCE_CHAIN_INVALIDATED",
        )

    if _event_already_consumed(
        session,
        actor_id=current.subject_actor_id,
        event_id=receipt.id,
    ):
        raise SuggestionReplyError("SUGGESTION_REPLY_EVENT_REUSED")

    current.status = "SUPERSEDED"
    current.updated_at = now_utc()
    value = current.object_json or {}
    context = current.context or {}
    target_state = (
        "INTERESTED"
        if decision is SuggestionDecision.INTERESTED
        else "DISMISSED"
    )
    row = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=current.subject_actor_id,
        subject_entity_id=None,
        predicate=current.predicate,
        object_type=current.object_type,
        object_text=current.object_text,
        object_actor_id=None,
        object_entity_id=None,
        object_json={
            **value,
            "lifecycle_state": target_state,
            "capability_name": None,
            "execution_requested": False,
            "grants_authority": False,
        },
        context={
            **context,
            "resolution_inbound_event_id": receipt.id,
            "resolution_kind": decision.value,
            "resolved_at": received_at.isoformat(),
        },
        confidence=current.confidence,
        sensitivity_class=current.sensitivity_class,
        source_quality=current.source_quality,
        valid_from=current.valid_from,
        valid_until=current.valid_until,
        status="ACTIVE",
        staleness_class=current.staleness_class,
        supersedes_claim_id=current.id,
        conflict_group_id=current.conflict_group_id,
        first_observed_at=current.first_observed_at,
        last_observed_at=received_at,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return SuggestionReplyResolution(
        suggestion_id=suggestion_id,
        decision=decision,
        lifecycle_claim_id=row.id,
        status="RESOLVED",
        reason_code=None,
    )


__all__ = [
    "SuggestionDecision",
    "SuggestionReplyError",
    "SuggestionReplyResolution",
    "parse_suggestion_reply",
    "resolve_explicit_suggestion_reply",
]
