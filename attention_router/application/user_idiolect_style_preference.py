from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from attention_router.application.platform.entities import record_fact
from attention_router.core.entities import EntityReference, FactClass
from attention_router.infrastructure.models import FactRow, InboundEventRow


STYLE_PREFERENCE_SOURCE_TYPE = "USER_DECLARATION"

_ALLOWED_VALUES: dict[str, frozenset[str]] = {
    "directness": frozenset({"low", "neutral", "high"}),
    "formality": frozenset({"low", "neutral", "high"}),
    "technical_depth": frozenset({"low", "medium", "high"}),
    "response_length": frozenset({"short", "medium", "long"}),
    "humor": frozenset({"none", "neutral", "occasional"}),
}


@dataclass(frozen=True, slots=True)
class ExplicitStylePreference:
    dimension: str
    value: str


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.strip().casefold())
    ascii_text = "".join(
        ch for ch in decomposed if not unicodedata.combining(ch)
    )
    return " ".join(re.sub(r"[^a-z0-9\s]", " ", ascii_text).split())


_PATTERNS: tuple[tuple[re.Pattern[str], ExplicitStylePreference], ...] = tuple(
    (re.compile(pattern), preference)
    for pattern, preference in (
        (
            r"(?:responda|responde|fale|fala)(?: comigo)? mais curt[oa]",
            ExplicitStylePreference("response_length", "short"),
        ),
        (
            r"(?:responda|responde|fale|fala)(?: comigo)? mais detalhad[oa]",
            ExplicitStylePreference("response_length", "long"),
        ),
        (
            r"(?:responda|responde|fale|fala)(?: comigo)? mais tecnic[oa]",
            ExplicitStylePreference("technical_depth", "high"),
        ),
        (
            r"(?:responda|responde|fale|fala)(?: comigo)? menos tecnic[oa]",
            ExplicitStylePreference("technical_depth", "low"),
        ),
        (
            r"(?:pode )?(?:ser|falar|fale|fala)(?: comigo)? mais informal",
            ExplicitStylePreference("formality", "low"),
        ),
        (
            r"(?:pode )?(?:ser|falar|fale|fala)(?: comigo)? mais formal",
            ExplicitStylePreference("formality", "high"),
        ),
        (
            r"(?:seja|seja comigo|responda|responde|fale|fala)(?: comigo)? mais diret[oa]",
            ExplicitStylePreference("directness", "high"),
        ),
        (
            r"(?:seja|seja comigo|responda|responde|fale|fala)(?: comigo)? menos diret[oa]",
            ExplicitStylePreference("directness", "low"),
        ),
        (
            r"(?:pode )?(?:usar|use)(?: um pouco de)? humor",
            ExplicitStylePreference("humor", "occasional"),
        ),
        (
            r"(?:sem humor|nao use humor)",
            ExplicitStylePreference("humor", "none"),
        ),
    )
)


def parse_explicit_style_preference(text: str) -> ExplicitStylePreference | None:
    if not isinstance(text, str) or not text.strip():
        return None
    if "?" in text:
        return None
    normalized = _normalize(text)
    matches = {
        preference
        for pattern, preference in _PATTERNS
        if pattern.fullmatch(normalized)
    }
    if len(matches) != 1:
        return None
    preference = next(iter(matches))
    if preference.value not in _ALLOWED_VALUES[preference.dimension]:
        return None
    return preference


def _active_same_dimension(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    dimension: str,
    stamp,
) -> list[FactRow]:
    predicate = f"communication.preference.{dimension}"
    superseded_ids = select(FactRow.supersedes_fact_id).where(
        FactRow.tenant_id == tenant_id,
        FactRow.subject_type == "ACTOR",
        FactRow.subject_id == actor_key,
        FactRow.predicate == predicate,
        FactRow.supersedes_fact_id.is_not(None),
    )
    return list(
        session.scalars(
            select(FactRow)
            .where(
                FactRow.tenant_id == tenant_id,
                FactRow.subject_type == "ACTOR",
                FactRow.subject_id == actor_key,
                FactRow.predicate == predicate,
                FactRow.fact_class
                == FactClass.USER_DECLARED_COMMUNICATION_PREFERENCE.value,
                FactRow.id.not_in(superseded_ids),
                or_(FactRow.valid_from.is_(None), FactRow.valid_from <= stamp),
                or_(FactRow.valid_until.is_(None), FactRow.valid_until > stamp),
            )
            .order_by(FactRow.observed_at.desc())
            .limit(20)
        ).all()
    )


def record_explicit_style_preference(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    source_event: InboundEventRow,
    preference: ExplicitStylePreference,
) -> tuple[FactRow, bool]:
    """Persist one authenticated owner-declared outbound style preference."""

    if source_event.tenant_id != tenant_id:
        raise ValueError("STYLE_PREFERENCE_TENANT_SCOPE_MISMATCH")
    payload = source_event.payload or {}
    metadata = payload.get("metadata") or {}
    if (
        payload.get("event_origin") != "OWNER_COMMAND"
        or payload.get("owner_authenticated") is not True
        or metadata.get("from_me") is not True
        or metadata.get("owner_self_chat") is not True
        or metadata.get("from_me_classification") != "OWNER_COMMAND"
        or metadata.get("final_from_me_classification") != "OWNER_COMMAND"
    ):
        raise ValueError("STYLE_PREFERENCE_OWNER_AUTHORITY_UNAVAILABLE")

    existing_source = session.scalar(
        select(FactRow).where(
            FactRow.tenant_id == tenant_id,
            FactRow.fact_class
            == FactClass.USER_DECLARED_COMMUNICATION_PREFERENCE.value,
            FactRow.source_type == STYLE_PREFERENCE_SOURCE_TYPE,
            FactRow.source_ref == source_event.id,
        )
    )
    if existing_source is not None:
        return existing_source, False

    active = _active_same_dimension(
        session,
        tenant_id=tenant_id,
        actor_key=actor_key,
        dimension=preference.dimension,
        stamp=source_event.received_at,
    )
    same = [
        row
        for row in active
        if (row.value_json or {}).get("value") == preference.value
    ]
    if len(same) == 1 and len(active) == 1:
        return same[0], False

    supersedes = active[0].id if len(active) == 1 else None
    fact = record_fact(
        session,
        tenant_id=tenant_id,
        subject=EntityReference(entity_type="ACTOR", entity_id=actor_key),
        predicate=f"communication.preference.{preference.dimension}",
        fact_class=FactClass.USER_DECLARED_COMMUNICATION_PREFERENCE,
        source_type=STYLE_PREFERENCE_SOURCE_TYPE,
        source_ref=source_event.id,
        confidence=1.0,
        value={
            "value": preference.value,
            "direction": "ANDY_TO_USER_PREFERENCE",
            "evidence_class": "USER_DECLARED",
            "reuse_policy": "EXPLICIT_REUSE",
            "generalization_scope": "PERSON",
            "evidence_confidence": 1.0,
            "generalization_confidence": 1.0,
        },
        valid_from=source_event.received_at,
        valid_until=None,
        supersedes_fact_id=supersedes,
        metadata_sanitized={
            "source_inbound_event_id": source_event.id,
            "sensitivity": "PRIVATE",
        },
    )
    return fact, True


def render_style_preference_confirmation(
    preference: ExplicitStylePreference,
    *,
    changed: bool,
) -> str:
    labels = {
        ("response_length", "short"): "Vou responder de forma mais curta.",
        ("response_length", "long"): "Vou responder com mais detalhes.",
        ("technical_depth", "high"): "Vou usar um nível mais técnico nas respostas.",
        ("technical_depth", "low"): "Vou reduzir o nível técnico das respostas.",
        ("formality", "low"): "Vou falar de forma mais informal.",
        ("formality", "high"): "Vou falar de forma mais formal.",
        ("directness", "high"): "Vou ser mais direta nas respostas.",
        ("directness", "low"): "Vou ser menos direta nas respostas.",
        ("humor", "occasional"): "Vou usar humor ocasionalmente.",
        ("humor", "none"): "Vou evitar humor nas respostas.",
    }
    text = labels[(preference.dimension, preference.value)]
    return text if changed else f"Essa preferência já está ativa. {text}"


__all__ = [
    "ExplicitStylePreference",
    "STYLE_PREFERENCE_SOURCE_TYPE",
    "parse_explicit_style_preference",
    "record_explicit_style_preference",
    "render_style_preference_confirmation",
]
