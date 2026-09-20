from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re
import unicodedata

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from attention_router.application.owner_control_semantic_registry import (
    OwnerSemanticRegistryError,
    normalize_semantic_parameters,
)
from attention_router.core.entities import FactClass
from attention_router.infrastructure.models import (
    ActorBindingRow,
    FactRow,
    MemoryActorRow,
    MemoryClaimRow,
)


PREDICATE = "idiolect.pragmatic_mapping"


@dataclass(frozen=True, slots=True)
class IdiolectInterpretationEvidence:
    fact_id: str
    semantic_intent_key: str
    parameters: dict[str, object]
    evidence_confidence: float
    generalization_confidence: float
    direction: str
    reuse_policy: str
    source_type: str
    source_ref: str | None
    observed_at: datetime
    valid_until: datetime | None


EXPLICIT_CONFIRMED_PRECEDENCE = 400
REPEATED_OBSERVED_PRECEDENCE = 200


@dataclass(frozen=True, slots=True)
class IdiolectContextEvidence:
    item_id: str
    evidence_kind: str
    predicate: str
    structured_value: dict[str, object]
    evidence_confidence: float
    generalization_confidence: float
    direction: str
    reuse_policy: str
    source_quality: str
    precedence: int
    observed_at: datetime
    valid_until: datetime | None


def normalize_user_expression(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.strip().casefold())
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9\s]", " ", ascii_text).split())


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def retrieve_idiolect_interpretation_evidence(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    utterance: str,
    source_channel: str,
    conversation_key_hash: str,
    limit: int = 8,
    now: datetime | None = None,
) -> tuple[IdiolectInterpretationEvidence, ...]:
    if limit < 1 or limit > 20:
        raise ValueError("IDIOLECT_RETRIEVAL_LIMIT_OUT_OF_RANGE")
    if not utterance.strip() or not source_channel.strip() or not conversation_key_hash.strip():
        return ()

    stamp = _utc(now or datetime.now(UTC))
    normalized = normalize_user_expression(utterance)
    superseded_ids = select(FactRow.supersedes_fact_id).where(
        FactRow.tenant_id == tenant_id,
        FactRow.subject_type == "ACTOR",
        FactRow.subject_id == actor_key,
        FactRow.predicate == PREDICATE,
        FactRow.supersedes_fact_id.is_not(None),
    )
    rows = session.scalars(
        select(FactRow)
        .where(
            FactRow.tenant_id == tenant_id,
            FactRow.subject_type == "ACTOR",
            FactRow.subject_id == actor_key,
            FactRow.predicate == PREDICATE,
            FactRow.fact_class == FactClass.USER_CONFIRMED_LANGUAGE.value,
            FactRow.id.not_in(superseded_ids),
            or_(FactRow.valid_from.is_(None), FactRow.valid_from <= stamp),
            or_(FactRow.valid_until.is_(None), FactRow.valid_until > stamp),
        )
        .order_by(FactRow.observed_at.desc())
        .limit(limit * 4)
    ).all()

    evidence: list[IdiolectInterpretationEvidence] = []
    for row in rows:
        value = row.value_json or {}
        scope = value.get("context_scope")
        if not isinstance(scope, dict):
            continue
        if value.get("normalized_expression") != normalized:
            continue
        if value.get("direction") != "USER_TO_ANDY_LANGUAGE":
            continue
        if value.get("evidence_class") != "EXPLICITLY_CONFIRMED":
            continue
        if value.get("reuse_policy") not in {"INTERPRET_ONLY", "CONTEXTUAL_REUSE", "EXPLICIT_REUSE"}:
            continue
        if value.get("generalization_scope") != "CONVERSATION":
            continue
        if scope.get("channel") != source_channel:
            continue
        if scope.get("conversation_key_hash") != conversation_key_hash:
            continue

        semantic_key = value.get("semantic_intent_key")
        parameters = value.get("parameters")
        if not isinstance(semantic_key, str) or not isinstance(parameters, dict):
            continue
        try:
            normalized_parameters = normalize_semantic_parameters(
                semantic_key,
                parameters,
            )
        except OwnerSemanticRegistryError:
            continue

        evidence.append(
            IdiolectInterpretationEvidence(
                fact_id=row.id,
                semantic_intent_key=semantic_key,
                parameters=normalized_parameters,
                evidence_confidence=float(value.get("evidence_confidence", row.confidence)),
                generalization_confidence=float(value.get("generalization_confidence", 0.0)),
                direction=str(value.get("direction")),
                reuse_policy=str(value.get("reuse_policy")),
                source_type=row.source_type,
                source_ref=row.source_ref,
                observed_at=row.observed_at,
                valid_until=row.valid_until,
            )
        )
        if len(evidence) >= limit:
            break

    return tuple(evidence)





def retrieve_idiolect_context(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    utterance: str,
    source_channel: str,
    conversation_key_hash: str,
    limit: int = 8,
    now: datetime | None = None,
) -> tuple[IdiolectContextEvidence, ...]:
    """Return bounded idiolect context with explicit evidence ranked first.

    Repeated observed patterns are available as contextual evidence but cannot
    become semantic parse results through this contract.
    """

    if limit < 1 or limit > 20:
        raise ValueError("IDIOLECT_RETRIEVAL_LIMIT_OUT_OF_RANGE")
    stamp = _utc(now or datetime.now(UTC))

    explicit = retrieve_idiolect_interpretation_evidence(
        session,
        tenant_id=tenant_id,
        actor_key=actor_key,
        utterance=utterance,
        source_channel=source_channel,
        conversation_key_hash=conversation_key_hash,
        limit=limit,
        now=stamp,
    )

    items: list[IdiolectContextEvidence] = [
        IdiolectContextEvidence(
            item_id=item.fact_id,
            evidence_kind="EXPLICIT_CONFIRMED_MAPPING",
            predicate=PREDICATE,
            structured_value={
                "semantic_intent_key": item.semantic_intent_key,
                "parameters": dict(item.parameters),
            },
            evidence_confidence=item.evidence_confidence,
            generalization_confidence=item.generalization_confidence,
            direction=item.direction,
            reuse_policy=item.reuse_policy,
            source_quality="EXPLICITLY_CONFIRMED",
            precedence=EXPLICIT_CONFIRMED_PRECEDENCE,
            observed_at=item.observed_at,
            valid_until=item.valid_until,
        )
        for item in explicit
    ]

    bindings = session.scalars(
        select(ActorBindingRow).where(
            ActorBindingRow.tenant_id == tenant_id,
            ActorBindingRow.actor_key == actor_key,
            ActorBindingRow.is_active.is_(True),
        )
    ).all()
    aliases = {actor_key, *(binding.external_actor_id for binding in bindings)}
    memory_actor_ids = session.scalars(
        select(MemoryActorRow.id).where(
            MemoryActorRow.tenant_id == tenant_id,
            MemoryActorRow.actor_key.in_(aliases),
        )
    ).all()

    if memory_actor_ids:
        claims = session.scalars(
            select(MemoryClaimRow)
            .where(
                MemoryClaimRow.subject_actor_id.in_(memory_actor_ids),
                MemoryClaimRow.status == "ACTIVE",
                MemoryClaimRow.source_quality == "REPEATED_OBSERVATION",
                MemoryClaimRow.sensitivity_class != "SECRET",
                or_(MemoryClaimRow.valid_from.is_(None), MemoryClaimRow.valid_from <= stamp),
                or_(MemoryClaimRow.valid_until.is_(None), MemoryClaimRow.valid_until > stamp),
            )
            .order_by(MemoryClaimRow.last_observed_at.desc())
            .limit(limit * 4)
        ).all()

        for claim in claims:
            value = claim.object_json or {}
            if value.get("direction") != "USER_TO_ANDY_LANGUAGE":
                continue
            if value.get("evidence_class") != "OBSERVED":
                continue
            if value.get("reuse_policy") != "INTERPRET_ONLY":
                continue
            if value.get("generalization_scope") != "PERSON":
                continue

            items.append(
                IdiolectContextEvidence(
                    item_id=claim.id,
                    evidence_kind="REPEATED_OBSERVED_PATTERN",
                    predicate=claim.predicate,
                    structured_value=dict(value),
                    evidence_confidence=float(claim.confidence),
                    generalization_confidence=float(
                        value.get("generalization_confidence", 0.0)
                    ),
                    direction=str(value.get("direction")),
                    reuse_policy=str(value.get("reuse_policy")),
                    source_quality=claim.source_quality,
                    precedence=REPEATED_OBSERVED_PRECEDENCE,
                    observed_at=claim.last_observed_at,
                    valid_until=claim.valid_until,
                )
            )

    items.sort(
        key=lambda item: (
            -item.precedence,
            -item.evidence_confidence,
            -item.generalization_confidence,
            -_utc(item.observed_at).timestamp(),
            item.item_id,
        )
    )
    return tuple(items[:limit])


def select_unique_confirmed_candidate(
    evidence: tuple[IdiolectInterpretationEvidence, ...],
    candidate_set: dict[str, object],
) -> dict[str, object] | None:
    candidates = candidate_set.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None

    evidence_meanings: dict[tuple[str, tuple[tuple[str, object], ...]], IdiolectInterpretationEvidence] = {}
    for item in evidence:
        if item.evidence_confidence < 1.0:
            continue
        signature = (
            item.semantic_intent_key,
            tuple(sorted(item.parameters.items())),
        )
        evidence_meanings[signature] = item

    if len(evidence_meanings) != 1:
        return None
    meaning = next(iter(evidence_meanings))

    matches: list[dict[str, object]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        semantic_key = candidate.get("semantic_intent_key")
        parameters = candidate.get("parameters")
        if not isinstance(semantic_key, str) or not isinstance(parameters, dict):
            continue
        signature = (semantic_key, tuple(sorted(parameters.items())))
        if signature == meaning:
            matches.append(candidate)

    if len(matches) != 1:
        return None
    return matches[0]


__all__ = [
    "EXPLICIT_CONFIRMED_PRECEDENCE",
    "IdiolectContextEvidence",
    "IdiolectInterpretationEvidence",
    "PREDICATE",
    "REPEATED_OBSERVED_PRECEDENCE",
    "normalize_user_expression",
    "retrieve_idiolect_context",
    "retrieve_idiolect_interpretation_evidence",
    "select_unique_confirmed_candidate",
]
