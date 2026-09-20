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
from attention_router.infrastructure.models import FactRow


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
    rows = session.scalars(
        select(FactRow)
        .where(
            FactRow.tenant_id == tenant_id,
            FactRow.subject_type == "ACTOR",
            FactRow.subject_id == actor_key,
            FactRow.predicate == PREDICATE,
            FactRow.fact_class == FactClass.USER_CONFIRMED_LANGUAGE.value,
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


__all__ = [
    "IdiolectInterpretationEvidence",
    "PREDICATE",
    "normalize_user_expression",
    "retrieve_idiolect_interpretation_evidence",
]
