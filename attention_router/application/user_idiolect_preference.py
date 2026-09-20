from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.platform.entities import record_fact
from attention_router.application.user_idiolect import normalize_user_expression
from attention_router.core.entities import EntityReference, FactClass
from attention_router.domain.models import now_utc
from attention_router.infrastructure.models import FactRow


class UserStylePreferenceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UserStylePreferenceCommand:
    dimension: str
    value: str
    confirmation: str


_PATTERNS: tuple[tuple[re.Pattern[str], UserStylePreferenceCommand], ...] = (
    (
        re.compile(
            r"^(?:responda|responde|fale|fala)(?: comigo)? mais curto$"
            r"|^quero respostas mais curtas$"
        ),
        UserStylePreferenceCommand(
            dimension="response_length",
            value="short",
            confirmation="Entendido. Vou responder mais curto.",
        ),
    ),
    (
        re.compile(
            r"^(?:responda|responde|fale|fala)(?: comigo)? mais detalhado$"
            r"|^quero respostas mais longas$"
        ),
        UserStylePreferenceCommand(
            dimension="response_length",
            value="long",
            confirmation="Entendido. Vou responder com mais detalhes.",
        ),
    ),
    (
        re.compile(
            r"^(?:fale|fala)(?: comigo)? mais tecnico(?: comigo)?$"
            r"|^(?:seja|pode ser) mais tecnico$"
        ),
        UserStylePreferenceCommand(
            dimension="technical_depth",
            value="high",
            confirmation="Entendido. Vou falar de forma mais técnica.",
        ),
    ),
    (
        re.compile(
            r"^(?:fale|fala)(?: comigo)? menos tecnico(?: comigo)?$"
            r"|^(?:fale|fala)(?: comigo)? mais simples(?: comigo)?$"
            r"|^(?:seja|pode ser) menos tecnico$"
        ),
        UserStylePreferenceCommand(
            dimension="technical_depth",
            value="low",
            confirmation="Entendido. Vou reduzir o nível técnico.",
        ),
    ),
    (
        re.compile(
            r"^(?:fale|fala)(?: comigo)? mais informal(?: comigo)?$"
            r"|^(?:seja|pode ser) mais informal$"
        ),
        UserStylePreferenceCommand(
            dimension="formality",
            value="low",
            confirmation="Entendido. Posso ser mais informal.",
        ),
    ),
    (
        re.compile(
            r"^(?:fale|fala)(?: comigo)? mais formal(?: comigo)?$"
            r"|^(?:seja|pode ser) mais formal$"
        ),
        UserStylePreferenceCommand(
            dimension="formality",
            value="high",
            confirmation="Entendido. Vou ser mais formal.",
        ),
    ),
    (
        re.compile(
            r"^(?:seja|responda|responde|fale|fala)(?: comigo)? mais direto(?: comigo)?$"
            r"|^(?:pode ser) mais direto$"
        ),
        UserStylePreferenceCommand(
            dimension="directness",
            value="high",
            confirmation="Entendido. Vou ser mais direto.",
        ),
    ),
    (
        re.compile(
            r"^(?:seja|responda|responde|fale|fala)(?: comigo)? menos direto(?: comigo)?$"
            r"|^(?:pode ser) menos direto$"
        ),
        UserStylePreferenceCommand(
            dimension="directness",
            value="low",
            confirmation="Entendido. Vou ser menos direto.",
        ),
    ),
    (
        re.compile(
            r"^(?:pode usar|use)(?: comigo)?(?: um pouco de)? humor$"
            r"|^(?:pode ser) mais bem humorado$"
        ),
        UserStylePreferenceCommand(
            dimension="humor",
            value="occasional",
            confirmation="Entendido. Vou usar humor ocasionalmente.",
        ),
    ),
    (
        re.compile(
            r"^sem humor$"
            r"|^(?:nao use|evite) humor$"
        ),
        UserStylePreferenceCommand(
            dimension="humor",
            value="none",
            confirmation="Entendido. Vou evitar humor.",
        ),
    ),
)


def parse_user_style_preference(
    text: str,
) -> UserStylePreferenceCommand | None:
    if not isinstance(text, str) or not text.strip():
        return None
    normalized = normalize_user_expression(text)
    for pattern, command in _PATTERNS:
        if pattern.fullmatch(normalized):
            return command
    return None


def _active_preference_facts(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    dimension: str,
) -> list[FactRow]:
    predicate = f"communication.preference.{dimension}"
    superseded_ids = select(FactRow.supersedes_fact_id).where(
        FactRow.tenant_id == tenant_id,
        FactRow.subject_type == "ACTOR",
        FactRow.subject_id == actor_key,
        FactRow.fact_class
        == FactClass.USER_DECLARED_COMMUNICATION_PREFERENCE.value,
        FactRow.predicate == predicate,
        FactRow.supersedes_fact_id.is_not(None),
    )
    stamp = now_utc()
    return list(
        session.scalars(
            select(FactRow)
            .where(
                FactRow.tenant_id == tenant_id,
                FactRow.subject_type == "ACTOR",
                FactRow.subject_id == actor_key,
                FactRow.fact_class
                == FactClass.USER_DECLARED_COMMUNICATION_PREFERENCE.value,
                FactRow.predicate == predicate,
                FactRow.id.not_in(superseded_ids),
                (FactRow.valid_from.is_(None) | (FactRow.valid_from <= stamp)),
                (FactRow.valid_until.is_(None) | (FactRow.valid_until > stamp)),
            )
            .order_by(FactRow.observed_at.desc())
        ).all()
    )


def persist_user_style_preference(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    command: UserStylePreferenceCommand,
    source_ref: str,
) -> FactRow:
    active = _active_preference_facts(
        session,
        tenant_id=tenant_id,
        actor_key=actor_key,
        dimension=command.dimension,
    )
    if len(active) > 1:
        raise UserStylePreferenceError(
            "USER_STYLE_PREFERENCE_ACTIVE_CONFLICT"
        )

    previous = active[0] if active else None
    if previous is not None and (previous.value_json or {}).get("value") == command.value:
        return previous

    stamp = now_utc()
    return record_fact(
        session,
        tenant_id=tenant_id,
        subject=EntityReference(entity_type="ACTOR", entity_id=actor_key),
        predicate=f"communication.preference.{command.dimension}",
        fact_class=FactClass.USER_DECLARED_COMMUNICATION_PREFERENCE,
        source_type="USER_DECLARATION",
        confidence=1.0,
        value={
            "value": command.value,
            "direction": "ANDY_TO_USER_PREFERENCE",
            "evidence_class": "USER_DECLARED",
            "reuse_policy": "EXPLICIT_REUSE",
            "generalization_scope": "PERSON",
            "evidence_confidence": 1.0,
            "generalization_confidence": 1.0,
        },
        source_ref=source_ref,
        valid_from=stamp,
        valid_until=None,
        supersedes_fact_id=previous.id if previous is not None else None,
        metadata_sanitized={
            "sensitivity": "PRIVATE",
            "preference_dimension": command.dimension,
        },
    )


__all__ = [
    "UserStylePreferenceCommand",
    "UserStylePreferenceError",
    "parse_user_style_preference",
    "persist_user_style_preference",
]
