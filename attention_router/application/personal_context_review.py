from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.domain.models import new_id, now_utc
from attention_router.application.personal_context_controls import (
    claim_is_owner_private,
)
from attention_router.infrastructure.models import (
    ActorBindingRow,
    InboundEventRow,
    MemoryActorRow,
    MemoryClaimRow,
)


_REVIEW = re.compile(
    r"^(?:mostrar revisão|mostrar revisao|mostrar essa revisão|mostrar essa revisao|ver revisão|ver revisao)$"
)


@dataclass(frozen=True, slots=True)
class ContextReviewResolution:
    suggestion_id: str | None
    lifecycle_claim_id: str | None
    status: str
    summary: str | None
    reason_code: str | None = None


class ContextReviewError(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def is_context_review_request(text: str) -> bool:
    normalized = " ".join(text.strip().lower().split())
    return _REVIEW.fullmatch(normalized) is not None


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


def _interested_suggestions(
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
            MemoryClaimRow.predicate == "context.suggestion.proactive",
            MemoryClaimRow.source_quality == "DERIVED_SUGGESTION",
            MemoryClaimRow.status == "ACTIVE",
        )
        .order_by(MemoryClaimRow.updated_at.desc(), MemoryClaimRow.id.desc())
    ).all()
    return tuple(
        row
        for row in rows
        if (row.object_json or {}).get("lifecycle_state") == "INTERESTED"
        and (row.object_json or {}).get("capability_name") is None
        and (row.object_json or {}).get("execution_requested") is False
        and (row.object_json or {}).get("grants_authority") is False
        and row.sensitivity_class != "SECRET"
        and row.valid_until is not None
        and _utc(row.valid_until) > now
    )


def _source_chain(
    session: Session,
    *,
    suggestion: MemoryClaimRow,
    now: datetime,
) -> tuple[MemoryClaimRow, MemoryClaimRow] | None:
    context = suggestion.context or {}
    anomaly = session.get(
        MemoryClaimRow,
        context.get("source_anomaly_claim_id"),
    )
    sequence = session.get(
        MemoryClaimRow,
        context.get("source_sequence_claim_id"),
    )
    if (
        anomaly is None
        or sequence is None
        or anomaly.subject_actor_id != suggestion.subject_actor_id
        or sequence.subject_actor_id != suggestion.subject_actor_id
        or anomaly.predicate != "context.pattern.sequence_anomaly"
        or sequence.predicate != "context.pattern.event_sequence"
        or anomaly.source_quality != "DERIVED_PATTERN"
        or sequence.source_quality != "DERIVED_PATTERN"
        or anomaly.status != "ACTIVE"
        or sequence.status != "ACTIVE"
        or anomaly.sensitivity_class == "SECRET"
        or sequence.sensitivity_class == "SECRET"
        or (
            anomaly.valid_until is not None
            and _utc(anomaly.valid_until) <= now
        )
        or (
            sequence.valid_until is not None
            and _utc(sequence.valid_until) <= now
        )
    ):
        return None

    if claim_is_owner_private(session, claim=sequence):
        return None

    anomaly_value = anomaly.object_json or {}
    sequence_value = sequence.object_json or {}
    if (
        anomaly_value.get("pattern_type") != "SEQUENCE_ANOMALY"
        or anomaly_value.get("anomaly_type") != "MISSING_EXPECTED_STEP"
        or anomaly_value.get("evidence_class") != "INFERRED"
        or anomaly_value.get("hypothesis_status") != "HYPOTHESIS"
        or anomaly_value.get("grants_authority") is not False
        or sequence_value.get("pattern_type") != "EVENT_SEQUENCE"
        or sequence_value.get("evidence_class") != "INFERRED"
        or sequence_value.get("hypothesis_status") != "HYPOTHESIS"
        or sequence_value.get("grants_authority") is not False
    ):
        return None
    return anomaly, sequence


def _bounded_summary(
    *,
    anomaly: MemoryClaimRow,
    sequence: MemoryClaimRow,
) -> str:
    sequence_context = sequence.context or {}
    anomaly_value = anomaly.object_json or {}

    occurrence_count = sequence_context.get("occurrence_count")
    support_ratio = sequence_context.get("support_ratio")
    expected_gap_seconds = anomaly_value.get("expected_gap_seconds")
    if (
        not isinstance(occurrence_count, int)
        or occurrence_count < 3
        or not isinstance(support_ratio, (int, float))
        or not isinstance(expected_gap_seconds, int)
        or expected_gap_seconds <= 0
    ):
        raise ContextReviewError("CONTEXT_REVIEW_SOURCE_CONTRACT_INVALID")

    support_percent = int(round(float(support_ratio) * 100))
    gap_minutes = max(1, int(round(expected_gap_seconds / 60)))
    return (
        "Revisão mínima: identifiquei uma rotina inferida de duas etapas, "
        f"sustentada por {occurrence_count} ocorrências "
        f"(suporte aproximado de {support_percent}%). "
        f"O intervalo típico entre as etapas é de cerca de {gap_minutes} minutos. "
        "Na ocorrência que gerou esta sugestão, a segunda etapa não apareceu "
        "dentro da janela esperada. "
        "Não estou exibindo mensagens, locais, contatos, identificadores, "
        "horários exatos nem conteúdo bruto das fontes."
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
            MemoryClaimRow.predicate == "context.suggestion.proactive",
            MemoryClaimRow.source_quality == "DERIVED_SUGGESTION",
        )
    ).all()
    return any(
        (row.context or {}).get("review_inbound_event_id") == event_id
        for row in rows
    )


def resolve_bounded_context_review(
    session: Session,
    *,
    receipt: InboundEventRow,
    actor_key: str,
    text: str,
) -> ContextReviewResolution | None:
    """Resolve an explicit structural-only review request.

    The review is limited to already-linked inferred claims. It never performs
    free-form Personal Context retrieval and never grants execution or broader
    disclosure authority.
    """

    if not is_context_review_request(text):
        return None
    if not _owner_event_is_valid(
        session,
        receipt=receipt,
        actor_key=actor_key,
    ):
        return ContextReviewResolution(
            suggestion_id=None,
            lifecycle_claim_id=None,
            status="OWNER_AUTHORITY_UNAVAILABLE",
            summary=None,
            reason_code="CONTEXT_REVIEW_OWNER_AUTHORITY_UNAVAILABLE",
        )

    stamp = _utc(receipt.received_at)
    candidates = _interested_suggestions(
        session,
        tenant_id=receipt.tenant_id,
        actor_key=actor_key,
        now=stamp,
    )
    if not candidates:
        return ContextReviewResolution(
            suggestion_id=None,
            lifecycle_claim_id=None,
            status="NO_ACTIVE_REVIEW",
            summary=None,
            reason_code="CONTEXT_REVIEW_NOT_AVAILABLE",
        )
    if len(candidates) != 1:
        raise ContextReviewError("CONTEXT_REVIEW_AMBIGUOUS")

    current = candidates[0]
    suggestion_id = (current.context or {}).get("suggestion_id")
    if not isinstance(suggestion_id, str) or not suggestion_id:
        raise ContextReviewError("CONTEXT_REVIEW_SUGGESTION_ID_MISSING")

    chain = _source_chain(
        session,
        suggestion=current,
        now=stamp,
    )
    if chain is None:
        return ContextReviewResolution(
            suggestion_id=suggestion_id,
            lifecycle_claim_id=None,
            status="SOURCE_INVALIDATED",
            summary=None,
            reason_code="CONTEXT_REVIEW_SOURCE_CHAIN_INVALIDATED",
        )

    if _event_already_consumed(
        session,
        actor_id=current.subject_actor_id,
        event_id=receipt.id,
    ):
        raise ContextReviewError("CONTEXT_REVIEW_EVENT_REUSED")

    anomaly, sequence = chain
    summary = _bounded_summary(
        anomaly=anomaly,
        sequence=sequence,
    )

    current.status = "SUPERSEDED"
    current.updated_at = now_utc()
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
            **(current.object_json or {}),
            "lifecycle_state": "REVIEWED",
            "capability_name": None,
            "execution_requested": False,
            "grants_authority": False,
            "review_disclosure_class": "STRUCTURAL_ONLY",
        },
        context={
            **(current.context or {}),
            "review_inbound_event_id": receipt.id,
            "reviewed_at": stamp.isoformat(),
            "review_disclosure_class": "STRUCTURAL_ONLY",
            "review_summary": summary,
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
        last_observed_at=stamp,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return ContextReviewResolution(
        suggestion_id=suggestion_id,
        lifecycle_claim_id=row.id,
        status="REVIEWED",
        summary=summary,
        reason_code=None,
    )


__all__ = [
    "ContextReviewError",
    "ContextReviewResolution",
    "is_context_review_request",
    "resolve_bounded_context_review",
]
