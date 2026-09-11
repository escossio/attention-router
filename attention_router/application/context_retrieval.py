"""Bounded, tenant-scoped retrieval over Personal Context.

V0 is deliberately deterministic and lexical.  It selects a small relevant
subset from the represented owner's Personal Context without granting any
disclosure or execution authority.  Embeddings and semantic retrieval can be
added behind this boundary later without changing callers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
import unicodedata
from typing import Any

from sqlalchemy.orm import Session

from attention_router.application.personal_context import (
    PersonalContextClaim,
    PersonalContextFact,
    PersonalContextSnapshot,
    build_personal_context,
)


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP_WORDS = {
    "a", "ao", "aos", "as", "com", "da", "das", "de", "do", "dos", "e",
    "em", "eu", "me", "meu", "meus", "minha", "minhas", "na", "nas", "no",
    "nos", "o", "os", "para", "por", "qual", "quais", "que", "se", "sobre",
    "um", "uma", "quando", "onde", "como", "tem", "tenho", "isso", "essa",
    "esse", "isto", "aquele", "aquela", "voce", "voces",
}

# Canonical predicates are intentionally provider/language neutral and currently
# use English tokens. V0 keeps retrieval deterministic while allowing a very
# small, explicit PT-BR bridge for canonical vocabulary. This is not semantic
# inference: aliases are reviewed data and matched terms always remain the
# original query terms for explainability.
_TOKEN_ALIASES: dict[str, set[str]] = {
    "primary": {"principal"},
    "principal": {"primary"},
    "payment": {"pagamento"},
    "pagamento": {"payment"},
    "work": {"trabalho"},
    "trabalho": {"work"},
    "branch": {"filial"},
    "filial": {"branch"},
}


@dataclass(frozen=True)
class RetrievedPersonalContextItem:
    kind: str
    item_id: str
    predicate: str
    value_text: str | None
    value_json: dict[str, Any] | None
    confidence: float
    relevance_score: float
    matched_terms: tuple[str, ...]
    provenance: dict[str, Any]

    def internal_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PersonalContextRetrievalResult:
    tenant_id: str
    represented_actor_id: str
    query: str
    items: tuple[RetrievedPersonalContextItem, ...]
    candidate_count: int
    retrieval_method: str = "LEXICAL_V0"

    def internal_payload(self) -> dict[str, Any]:
        return asdict(self)


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _tokens(value: str | None) -> set[str]:
    if not value:
        return set()
    return {
        token
        for token in _TOKEN_RE.findall(_normalize(value))
        if token not in _STOP_WORDS
    }


def _expand_candidate_tokens(tokens: set[str]) -> set[str]:
    expanded = set(tokens)
    for token in tokens:
        expanded.update(_TOKEN_ALIASES.get(token, ()))
    return expanded


def _json_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        return " ".join(f"{key} {_json_text(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_json_text(item) for item in value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _candidate_score(
    *,
    query_tokens: set[str],
    predicate: str,
    searchable_value: str,
    confidence: float,
) -> tuple[float, tuple[str, ...]]:
    predicate_tokens = _expand_candidate_tokens(
        _tokens(predicate.replace(".", " ").replace("_", " "))
    )
    value_tokens = _expand_candidate_tokens(_tokens(searchable_value))
    candidate_tokens = predicate_tokens | value_tokens
    matched = query_tokens & candidate_tokens
    if not matched:
        return 0.0, ()

    query_size = max(1, len(query_tokens))
    coverage = len(matched) / query_size
    predicate_coverage = len(query_tokens & predicate_tokens) / query_size
    value_coverage = len(query_tokens & value_tokens) / query_size
    score = (
        coverage * 0.55
        + predicate_coverage * 0.25
        + value_coverage * 0.15
        + max(0.0, min(confidence, 1.0)) * 0.05
    )
    return round(score, 6), tuple(sorted(matched))


def _claim_item(
    claim: PersonalContextClaim,
    query_tokens: set[str],
) -> RetrievedPersonalContextItem | None:
    searchable = " ".join(filter(None, [claim.value_text, _json_text(claim.value_json)]))
    score, matched = _candidate_score(
        query_tokens=query_tokens,
        predicate=claim.predicate,
        searchable_value=searchable,
        confidence=claim.confidence,
    )
    if score <= 0:
        return None
    return RetrievedPersonalContextItem(
        kind="MEMORY_CLAIM",
        item_id=claim.claim_id,
        predicate=claim.predicate,
        value_text=claim.value_text,
        value_json=claim.value_json,
        confidence=claim.confidence,
        relevance_score=score,
        matched_terms=matched,
        provenance={
            "source_quality": claim.source_quality,
            "staleness_class": claim.staleness_class,
            "first_observed_at": claim.first_observed_at.isoformat(),
            "last_observed_at": claim.last_observed_at.isoformat(),
        },
    )


def _fact_item(
    fact: PersonalContextFact,
    query_tokens: set[str],
) -> RetrievedPersonalContextItem | None:
    score, matched = _candidate_score(
        query_tokens=query_tokens,
        predicate=fact.predicate,
        searchable_value=_json_text(fact.value_json),
        confidence=fact.confidence,
    )
    if score <= 0:
        return None
    return RetrievedPersonalContextItem(
        kind="FACT",
        item_id=fact.fact_id,
        predicate=fact.predicate,
        value_text=None,
        value_json=fact.value_json,
        confidence=fact.confidence,
        relevance_score=score,
        matched_terms=matched,
        provenance={
            "fact_class": fact.fact_class,
            "source_type": fact.source_type,
            "source_ref": fact.source_ref,
            "observed_at": fact.observed_at.isoformat(),
        },
    )


def rank_personal_context(
    snapshot: PersonalContextSnapshot,
    query: str,
    *,
    limit: int = 8,
) -> PersonalContextRetrievalResult:
    """Return only Personal Context items with deterministic lexical evidence."""
    if limit < 1 or limit > 50:
        raise ValueError("CONTEXT_RETRIEVAL_LIMIT_OUT_OF_RANGE")
    query_tokens = _tokens(query)
    if not query_tokens:
        return PersonalContextRetrievalResult(
            tenant_id=snapshot.tenant_id,
            represented_actor_id=snapshot.represented_actor_id,
            query=query,
            items=(),
            candidate_count=len(snapshot.claims) + len(snapshot.facts),
        )

    candidates: list[RetrievedPersonalContextItem] = []
    for claim in snapshot.claims:
        if item := _claim_item(claim, query_tokens):
            candidates.append(item)
    for fact in snapshot.facts:
        if item := _fact_item(fact, query_tokens):
            candidates.append(item)

    candidates.sort(
        key=lambda item: (
            -item.relevance_score,
            -item.confidence,
            item.kind,
            item.predicate,
            item.item_id,
        )
    )
    return PersonalContextRetrievalResult(
        tenant_id=snapshot.tenant_id,
        represented_actor_id=snapshot.represented_actor_id,
        query=query,
        items=tuple(candidates[:limit]),
        candidate_count=len(snapshot.claims) + len(snapshot.facts),
    )


def retrieve_personal_context(
    session: Session,
    tenant_id: str,
    query: str,
    *,
    limit: int = 8,
    candidate_limit: int = 200,
) -> PersonalContextRetrievalResult:
    """Build tenant-scoped Personal Context and return its relevant subset."""
    if candidate_limit < limit or candidate_limit > 500:
        raise ValueError("CONTEXT_RETRIEVAL_CANDIDATE_LIMIT_OUT_OF_RANGE")
    snapshot = build_personal_context(session, tenant_id, limit=candidate_limit)
    return rank_personal_context(snapshot, query, limit=limit)
