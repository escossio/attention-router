from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from attention_router.core.entities import FactClass
from attention_router.infrastructure.models import (
    ActorBindingRow,
    FactRow,
    MemoryActorRow,
    MemoryClaimRow,
)


EXPLICIT_STYLE_PRECEDENCE: Final = 400
OBSERVED_STYLE_PRECEDENCE: Final = 200

_STYLE_DEFAULTS: Final[dict[str, str]] = {
    "directness": "neutral",
    "formality": "neutral",
    "technical_depth": "medium",
    "response_length": "medium",
    "humor": "neutral",
}

_STYLE_ALLOWED: Final[dict[str, frozenset[str]]] = {
    "directness": frozenset({"low", "neutral", "high"}),
    "formality": frozenset({"low", "neutral", "high"}),
    "technical_depth": frozenset({"low", "medium", "high"}),
    "response_length": frozenset({"short", "medium", "long"}),
    "humor": frozenset({"none", "neutral", "occasional"}),
}


@dataclass(frozen=True, slots=True)
class _StyleEvidence:
    dimension: str
    value: str
    precedence: int
    source: str


@dataclass(frozen=True, slots=True)
class ResponseStyleProfile:
    directness: str = "neutral"
    formality: str = "neutral"
    technical_depth: str = "medium"
    response_length: str = "medium"
    humor: str = "neutral"
    sources: tuple[tuple[str, str], ...] = ()
    explicit_preference_count: int = 0
    observed_style_signal_count: int = 0
    conflict_dimensions: tuple[str, ...] = ()

    @property
    def adaptation_applied(self) -> bool:
        return any(
            getattr(self, dimension) != default
            for dimension, default in _STYLE_DEFAULTS.items()
        )

    def prompt_payload(self) -> dict[str, object]:
        return {
            "dimensions": {
                "directness": self.directness,
                "formality": self.formality,
                "technical_depth": self.technical_depth,
                "response_length": self.response_length,
                "humor": self.humor,
            },
            "sources": dict(self.sources),
            "adaptation_applied": self.adaptation_applied,
            "explicit_preference_count": self.explicit_preference_count,
            "observed_style_signal_count": self.observed_style_signal_count,
            "conflict_dimensions": list(self.conflict_dimensions),
            "constraints": [
                "abstract_dimensions_only",
                "no_phrase_mimicry",
                "no_profanity_inference",
                "no_intimate_vocative_inference",
                "never_override_policy_or_safety",
            ],
        }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _style_value(value: dict[str, object], dimension: str) -> str | None:
    raw = value.get("style_value", value.get("value", value.get("level")))
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().casefold()
    return normalized if normalized in _STYLE_ALLOWED[dimension] else None


def _active_explicit_preferences(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    stamp: datetime,
) -> list[_StyleEvidence]:
    superseded_ids = select(FactRow.supersedes_fact_id).where(
        FactRow.tenant_id == tenant_id,
        FactRow.subject_type == "ACTOR",
        FactRow.subject_id == actor_key,
        FactRow.supersedes_fact_id.is_not(None),
    )
    rows = session.scalars(
        select(FactRow)
        .where(
            FactRow.tenant_id == tenant_id,
            FactRow.subject_type == "ACTOR",
            FactRow.subject_id == actor_key,
            FactRow.fact_class
            == FactClass.USER_DECLARED_COMMUNICATION_PREFERENCE.value,
            FactRow.id.not_in(superseded_ids),
            or_(FactRow.valid_from.is_(None), FactRow.valid_from <= stamp),
            or_(FactRow.valid_until.is_(None), FactRow.valid_until > stamp),
        )
        .order_by(FactRow.observed_at.desc())
        .limit(40)
    ).all()

    evidence: list[_StyleEvidence] = []
    for row in rows:
        prefix = "communication.preference."
        if not row.predicate.startswith(prefix):
            continue
        dimension = row.predicate.removeprefix(prefix)
        if dimension not in _STYLE_ALLOWED:
            continue
        value = row.value_json or {}
        if value.get("direction") != "ANDY_TO_USER_PREFERENCE":
            continue
        if value.get("evidence_class") != "USER_DECLARED":
            continue
        if value.get("reuse_policy") != "EXPLICIT_REUSE":
            continue
        style_value = _style_value(value, dimension)
        if style_value is None:
            continue
        evidence.append(
            _StyleEvidence(
                dimension=dimension,
                value=style_value,
                precedence=EXPLICIT_STYLE_PRECEDENCE,
                source="EXPLICIT_USER_PREFERENCE",
            )
        )
    return evidence


def _active_observed_style_signals(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    stamp: datetime,
) -> list[_StyleEvidence]:
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
    if not memory_actor_ids:
        return []

    rows = session.scalars(
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
        .limit(40)
    ).all()

    evidence: list[_StyleEvidence] = []
    for row in rows:
        value = row.object_json or {}
        if value.get("evidence_class") != "OBSERVED":
            continue
        if value.get("reuse_policy") != "STYLE_SIGNAL":
            continue
        if value.get("direction") != "USER_TO_ANDY_LANGUAGE":
            continue
        dimension = value.get("style_dimension")
        if not isinstance(dimension, str) or dimension not in _STYLE_ALLOWED:
            continue
        style_value = _style_value(value, dimension)
        if style_value is None:
            continue
        evidence.append(
            _StyleEvidence(
                dimension=dimension,
                value=style_value,
                precedence=OBSERVED_STYLE_PRECEDENCE,
                source="REPEATED_OBSERVED_STYLE_SIGNAL",
            )
        )
    return evidence


def build_response_style_profile(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    now: datetime | None = None,
) -> ResponseStyleProfile:
    """Build a bounded abstract style profile for Andy's outbound response.

    Raw user phrases are never returned. INTERPRET_ONLY observations cannot
    influence this profile.
    """

    stamp = _utc(now or datetime.now(UTC))
    explicit = _active_explicit_preferences(
        session,
        tenant_id=tenant_id,
        actor_key=actor_key,
        stamp=stamp,
    )
    observed = _active_observed_style_signals(
        session,
        tenant_id=tenant_id,
        actor_key=actor_key,
        stamp=stamp,
    )
    all_evidence = [*explicit, *observed]

    resolved = dict(_STYLE_DEFAULTS)
    sources = {dimension: "DEFAULT" for dimension in _STYLE_DEFAULTS}
    conflicts: list[str] = []

    for dimension in _STYLE_DEFAULTS:
        matches = [item for item in all_evidence if item.dimension == dimension]
        if not matches:
            continue
        highest = max(item.precedence for item in matches)
        top = [item for item in matches if item.precedence == highest]
        values = {item.value for item in top}
        if len(values) != 1:
            conflicts.append(dimension)
            continue
        resolved[dimension] = next(iter(values))
        sources[dimension] = top[0].source

    return ResponseStyleProfile(
        directness=resolved["directness"],
        formality=resolved["formality"],
        technical_depth=resolved["technical_depth"],
        response_length=resolved["response_length"],
        humor=resolved["humor"],
        sources=tuple((dimension, sources[dimension]) for dimension in _STYLE_DEFAULTS),
        explicit_preference_count=len(explicit),
        observed_style_signal_count=len(observed),
        conflict_dimensions=tuple(sorted(conflicts)),
    )


__all__ = [
    "EXPLICIT_STYLE_PRECEDENCE",
    "OBSERVED_STYLE_PRECEDENCE",
    "ResponseStyleProfile",
    "build_response_style_profile",
]
