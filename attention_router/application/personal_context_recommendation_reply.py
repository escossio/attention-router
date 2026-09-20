from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.personal_context_recommendation_lifecycle import (
    RECOMMENDATION_CLAIM_PREDICATE,
    RECOMMENDATION_SOURCE_QUALITY,
    RecommendationDecision,
    resolve_context_recommendation,
)
from attention_router.application.personal_context_runtime import (
    PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION,
)
from attention_router.infrastructure.models import (
    InboundEventRow,
    MemoryActorRow,
    MemoryClaimRow,
    OutboxMessageRow,
)


_ACCEPT = re.compile(
    r"^(?:sim|sim[, ]+pode|pode sim|aceito|quero|crie o lembrete|pode criar)$"
)
_DISMISS = re.compile(
    r"^(?:nao|não|nao obrigado|não obrigado|dispenso|deixa pra la|deixa pra lá)$"
)


@dataclass(frozen=True, slots=True)
class RecommendationReplyResolution:
    recommendation_id: str
    decision: RecommendationDecision
    lifecycle_claim_id: str


class RecommendationReplyError(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_recommendation_reply(text: str) -> RecommendationDecision | None:
    normalized = " ".join(text.strip().lower().split())
    if _ACCEPT.fullmatch(normalized):
        return RecommendationDecision.ACCEPT
    if _DISMISS.fullmatch(normalized):
        return RecommendationDecision.DISMISS
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
            MemoryClaimRow.predicate == RECOMMENDATION_CLAIM_PREDICATE,
            MemoryClaimRow.source_quality == RECOMMENDATION_SOURCE_QUALITY,
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
    recommendation_claim: MemoryClaimRow,
    external_actor_id: str,
    received_at: datetime,
) -> OutboxMessageRow | None:
    recommendation_id = (recommendation_claim.context or {}).get(
        "recommendation_id"
    )
    if not isinstance(recommendation_id, str):
        return None
    rows = session.scalars(
        select(OutboxMessageRow)
        .where(
            OutboxMessageRow.action_type
            == PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION,
            OutboxMessageRow.status == "DONE",
            OutboxMessageRow.completed_at.is_not(None),
        )
        .order_by(OutboxMessageRow.completed_at.desc(), OutboxMessageRow.id.desc())
    ).all()
    matching = [
        row
        for row in rows
        if row.payload.get("recommendation_id") == recommendation_id
        and row.payload.get("recommendation_claim_id") == recommendation_claim.id
        and row.payload.get("external_actor_id") == external_actor_id
        and _utc(row.completed_at) <= received_at
    ]
    if len(matching) != 1:
        return None
    return matching[0]


def resolve_explicit_recommendation_reply(
    session: Session,
    *,
    receipt: InboundEventRow,
    actor_key: str,
    text: str,
) -> RecommendationReplyResolution | None:
    """Resolve one unambiguous delivered proposal from an explicit owner reply."""

    decision = parse_recommendation_reply(text)
    if decision is None:
        return None

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
    ):
        return None

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
            recommendation_claim=proposal,
            external_actor_id=external_actor_id,
            received_at=received_at,
        ) is not None:
            candidates.append(proposal)

    if not candidates:
        return None
    if len(candidates) != 1:
        raise RecommendationReplyError(
            "RECOMMENDATION_REPLY_AMBIGUOUS"
        )

    recommendation_id = (candidates[0].context or {}).get("recommendation_id")
    if not isinstance(recommendation_id, str):
        raise RecommendationReplyError(
            "RECOMMENDATION_ID_MISSING"
        )

    terminal, _changed = resolve_context_recommendation(
        session,
        recommendation_id=recommendation_id,
        tenant_id=receipt.tenant_id,
        actor_key=actor_key,
        decision=decision,
        resolution_event=receipt,
        now=received_at,
    )
    return RecommendationReplyResolution(
        recommendation_id=recommendation_id,
        decision=decision,
        lifecycle_claim_id=terminal.id,
    )


__all__ = [
    "RecommendationReplyError",
    "RecommendationReplyResolution",
    "parse_recommendation_reply",
    "resolve_explicit_recommendation_reply",
]
