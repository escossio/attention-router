from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    ActorBindingRow,
    InboundEventRow,
    MemoryActorRow,
    MemoryClaimRow,
)


CONTEXT_CONTROL_PREDICATE: Final = "context.control.owner_policy"
CONTEXT_CONTROL_SOURCE_QUALITY: Final = "USER_DECLARED"


class ContextControlCommandKind(StrEnum):
    SET_PRIVATE = "SET_PRIVATE"
    CLEAR_PRIVATE = "CLEAR_PRIVATE"
    SET_NON_ACTIONABLE = "SET_NON_ACTIONABLE"
    CLEAR_NON_ACTIONABLE = "CLEAR_NON_ACTIONABLE"


@dataclass(frozen=True, slots=True)
class ContextControlCommand:
    kind: ContextControlCommandKind


@dataclass(frozen=True, slots=True)
class ContextControlPolicy:
    hypothesis_id: str
    private: bool
    non_actionable: bool
    control_claim_id: str | None


@dataclass(frozen=True, slots=True)
class ContextControlResolution:
    hypothesis_id: str
    control_claim_id: str
    command: ContextControlCommandKind
    private: bool
    non_actionable: bool
    changed: bool


class ContextControlError(RuntimeError):
    pass


_PRIVATE = re.compile(
    r"^(?:marcar este contexto como privado|deixar este contexto privado)$"
)
_CLEAR_PRIVATE = re.compile(
    r"^(?:remover marcação privada deste contexto|remover marcacao privada deste contexto)$"
)
_NON_ACTIONABLE = re.compile(
    r"^(?:não usar este contexto para ações|nao usar este contexto para acoes|marcar este contexto como não acionável|marcar este contexto como nao acionavel)$"
)
_CLEAR_NON_ACTIONABLE = re.compile(
    r"^(?:permitir sugestões com este contexto|permitir sugestoes com este contexto)$"
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_context_control(text: str) -> ContextControlCommand | None:
    normalized = " ".join(text.strip().lower().split())
    if _PRIVATE.fullmatch(normalized):
        return ContextControlCommand(ContextControlCommandKind.SET_PRIVATE)
    if _CLEAR_PRIVATE.fullmatch(normalized):
        return ContextControlCommand(ContextControlCommandKind.CLEAR_PRIVATE)
    if _NON_ACTIONABLE.fullmatch(normalized):
        return ContextControlCommand(ContextControlCommandKind.SET_NON_ACTIONABLE)
    if _CLEAR_NON_ACTIONABLE.fullmatch(normalized):
        return ContextControlCommand(ContextControlCommandKind.CLEAR_NON_ACTIONABLE)
    return None


def _owner_event_is_valid(
    session: Session,
    *,
    event: InboundEventRow,
    actor_key: str,
) -> bool:
    payload = event.payload or {}
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
            ActorBindingRow.tenant_id == event.tenant_id,
            ActorBindingRow.actor_key == actor_key,
            ActorBindingRow.source == event.source,
            ActorBindingRow.external_actor_id == external_actor_id,
            ActorBindingRow.is_active.is_(True),
        )
    )
    return binding is not None


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
        raise ContextControlError("CONTEXT_CONTROL_ACTOR_NOT_FOUND")
    return row


def _reviewed_suggestions(
    session: Session,
    *,
    actor_id: str,
    now: datetime,
) -> tuple[MemoryClaimRow, ...]:
    rows = session.scalars(
        select(MemoryClaimRow)
        .where(
            MemoryClaimRow.subject_actor_id == actor_id,
            MemoryClaimRow.predicate == "context.suggestion.proactive",
            MemoryClaimRow.source_quality == "DERIVED_SUGGESTION",
        )
        .order_by(MemoryClaimRow.updated_at.desc(), MemoryClaimRow.id.desc())
    ).all()
    candidates = [
        row
        for row in rows
        if (row.object_json or {}).get("lifecycle_state") == "REVIEWED"
        and (row.object_json or {}).get("capability_name") is None
        and (row.object_json or {}).get("execution_requested") is False
        and (row.object_json or {}).get("grants_authority") is False
        and row.sensitivity_class != "SECRET"
    ]
    if not candidates:
        return ()
    if (
        len(candidates) > 1
        and candidates[0].updated_at == candidates[1].updated_at
    ):
        return tuple(candidates[:2])
    return (candidates[0],)


def _target_hypothesis_id(
    session: Session,
    *,
    reviewed: MemoryClaimRow,
) -> tuple[str, MemoryClaimRow]:
    sequence_claim_id = (reviewed.context or {}).get("source_sequence_claim_id")
    if not isinstance(sequence_claim_id, str):
        raise ContextControlError("CONTEXT_CONTROL_SOURCE_SEQUENCE_MISSING")
    sequence = session.get(MemoryClaimRow, sequence_claim_id)
    if (
        sequence is None
        or sequence.subject_actor_id != reviewed.subject_actor_id
        or sequence.predicate != "context.pattern.event_sequence"
        or sequence.source_quality != "DERIVED_PATTERN"
    ):
        raise ContextControlError("CONTEXT_CONTROL_SOURCE_SEQUENCE_INVALID")
    hypothesis_id = (sequence.context or {}).get("hypothesis_id")
    if not isinstance(hypothesis_id, str) or not hypothesis_id:
        raise ContextControlError("CONTEXT_CONTROL_HYPOTHESIS_ID_MISSING")
    return hypothesis_id, sequence


def active_context_control_policy(
    session: Session,
    *,
    actor_id: str,
    hypothesis_id: str,
) -> ContextControlPolicy:
    rows = session.scalars(
        select(MemoryClaimRow)
        .where(
            MemoryClaimRow.subject_actor_id == actor_id,
            MemoryClaimRow.predicate == CONTEXT_CONTROL_PREDICATE,
            MemoryClaimRow.source_quality == CONTEXT_CONTROL_SOURCE_QUALITY,
            MemoryClaimRow.status == "ACTIVE",
        )
        .order_by(MemoryClaimRow.updated_at.desc(), MemoryClaimRow.id.desc())
    ).all()
    matching = [
        row
        for row in rows
        if (row.context or {}).get("target_hypothesis_id") == hypothesis_id
    ]
    if len(matching) > 1:
        raise ContextControlError("CONTEXT_CONTROL_ACTIVE_CONFLICT")
    if not matching:
        return ContextControlPolicy(
            hypothesis_id=hypothesis_id,
            private=False,
            non_actionable=False,
            control_claim_id=None,
        )
    row = matching[0]
    value = row.object_json or {}
    return ContextControlPolicy(
        hypothesis_id=hypothesis_id,
        private=value.get("privacy") == "PRIVATE",
        non_actionable=value.get("actionability") == "NON_ACTIONABLE",
        control_claim_id=row.id,
    )


def _claim_hypothesis_id(
    session: Session,
    claim: MemoryClaimRow,
    *,
    seen: set[str] | None = None,
) -> str | None:
    seen = set() if seen is None else seen
    if claim.id in seen:
        return None
    seen.add(claim.id)
    context = claim.context or {}

    direct = context.get("hypothesis_id")
    if isinstance(direct, str) and direct:
        return direct
    source_hypothesis = context.get("source_hypothesis_id")
    if isinstance(source_hypothesis, str) and source_hypothesis:
        return source_hypothesis

    for key in (
        "source_sequence_claim_id",
        "source_anomaly_claim_id",
        "source_claim_id",
    ):
        source_id = context.get(key)
        if not isinstance(source_id, str):
            continue
        source = session.get(MemoryClaimRow, source_id)
        if source is None:
            continue
        resolved = _claim_hypothesis_id(session, source, seen=seen)
        if resolved is not None:
            return resolved
    return None


def context_control_policy_for_claim(
    session: Session,
    *,
    claim: MemoryClaimRow,
) -> ContextControlPolicy | None:
    hypothesis_id = _claim_hypothesis_id(session, claim)
    if hypothesis_id is None:
        return None
    return active_context_control_policy(
        session,
        actor_id=claim.subject_actor_id,
        hypothesis_id=hypothesis_id,
    )


def claim_is_owner_private(
    session: Session,
    *,
    claim: MemoryClaimRow,
) -> bool:
    policy = context_control_policy_for_claim(session, claim=claim)
    return bool(policy and policy.private)


def claim_is_owner_non_actionable(
    session: Session,
    *,
    claim: MemoryClaimRow,
) -> bool:
    policy = context_control_policy_for_claim(session, claim=claim)
    return bool(policy and policy.non_actionable)


def apply_context_control(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    command: ContextControlCommand,
    control_event: InboundEventRow,
    now: datetime | None = None,
) -> ContextControlResolution:
    stamp = _utc(now or control_event.received_at)
    if control_event.tenant_id != tenant_id:
        raise ContextControlError("CONTEXT_CONTROL_TENANT_MISMATCH")
    if not _owner_event_is_valid(
        session,
        event=control_event,
        actor_key=actor_key,
    ):
        raise ContextControlError("CONTEXT_CONTROL_OWNER_AUTHORITY_UNAVAILABLE")

    actor = _actor(
        session,
        tenant_id=tenant_id,
        actor_key=actor_key,
    )
    reviewed = _reviewed_suggestions(
        session,
        actor_id=actor.id,
        now=stamp,
    )
    if not reviewed:
        raise ContextControlError("CONTEXT_CONTROL_REVIEW_NOT_AVAILABLE")
    if len(reviewed) != 1:
        raise ContextControlError("CONTEXT_CONTROL_REVIEW_AMBIGUOUS")

    source_review = reviewed[0]
    hypothesis_id, source_sequence = _target_hypothesis_id(
        session,
        reviewed=source_review,
    )
    current = active_context_control_policy(
        session,
        actor_id=actor.id,
        hypothesis_id=hypothesis_id,
    )
    private = current.private
    non_actionable = current.non_actionable

    if command.kind is ContextControlCommandKind.SET_PRIVATE:
        private = True
    elif command.kind is ContextControlCommandKind.CLEAR_PRIVATE:
        private = False
    elif command.kind is ContextControlCommandKind.SET_NON_ACTIONABLE:
        non_actionable = True
    elif command.kind is ContextControlCommandKind.CLEAR_NON_ACTIONABLE:
        non_actionable = False
    else:
        raise ContextControlError("CONTEXT_CONTROL_COMMAND_UNSUPPORTED")

    if (
        current.control_claim_id is not None
        and private == current.private
        and non_actionable == current.non_actionable
    ):
        existing = session.get(MemoryClaimRow, current.control_claim_id)
        assert existing is not None
        return ContextControlResolution(
            hypothesis_id=hypothesis_id,
            control_claim_id=existing.id,
            command=command.kind,
            private=private,
            non_actionable=non_actionable,
            changed=False,
        )

    previous = (
        session.get(MemoryClaimRow, current.control_claim_id)
        if current.control_claim_id is not None
        else None
    )
    if previous is not None:
        previous.status = "SUPERSEDED"
        previous.updated_at = now_utc()

    row = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=actor.id,
        subject_entity_id=None,
        predicate=CONTEXT_CONTROL_PREDICATE,
        object_type="JSON",
        object_text=None,
        object_actor_id=None,
        object_entity_id=None,
        object_json={
            "control_type": "OWNER_CONTEXT_POLICY",
            "privacy": "PRIVATE" if private else "DEFAULT",
            "actionability": (
                "NON_ACTIONABLE" if non_actionable else "DEFAULT"
            ),
            "evidence_class": "USER_DECLARED",
            "grants_authority": False,
            "grants_disclosure_authority": False,
        },
        context={
            "target_kind": "PATTERN_HYPOTHESIS",
            "target_hypothesis_id": hypothesis_id,
            "target_sequence_claim_id": source_sequence.id,
            "source_review_claim_id": source_review.id,
            "control_event_id": control_event.id,
            "command_kind": command.kind.value,
        },
        confidence=1.0,
        sensitivity_class="PRIVATE",
        source_quality=CONTEXT_CONTROL_SOURCE_QUALITY,
        valid_from=stamp,
        valid_until=None,
        status="ACTIVE",
        staleness_class="STABLE",
        supersedes_claim_id=previous.id if previous is not None else None,
        conflict_group_id=None,
        first_observed_at=stamp,
        last_observed_at=stamp,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return ContextControlResolution(
        hypothesis_id=hypothesis_id,
        control_claim_id=row.id,
        command=command.kind,
        private=private,
        non_actionable=non_actionable,
        changed=True,
    )


__all__ = [
    "CONTEXT_CONTROL_PREDICATE",
    "CONTEXT_CONTROL_SOURCE_QUALITY",
    "ContextControlCommand",
    "ContextControlCommandKind",
    "ContextControlError",
    "ContextControlPolicy",
    "ContextControlResolution",
    "active_context_control_policy",
    "apply_context_control",
    "claim_is_owner_non_actionable",
    "claim_is_owner_private",
    "context_control_policy_for_claim",
    "parse_context_control",
]
