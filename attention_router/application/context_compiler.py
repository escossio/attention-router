"""Deterministic graph-aware Context Compiler V1.

V2F compiles a small, explainable context packet from the tenant-scoped
Cognitive Graph. Retrieval remains knowledge selection only: it grants no
authority, disclosure permission, execution permission, or truth promotion.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import math
from typing import Any, Final, Iterable

from sqlalchemy.orm import Session

from attention_router.application.cognitive_graph import build_cognitive_graph_slice
from attention_router.application.context_retrieval import (
    _candidate_score,
    _json_text,
    _tokens,
    retrieve_personal_context,
)
from attention_router.domain.cognitive_graph import (
    CognitiveEdge,
    CognitiveGraphSlice,
    CognitiveNode,
    CognitiveRelationKind,
)


MAX_HOPS: Final = 4
MAX_ITEM_BUDGET: Final = 50
MAX_TOKEN_BUDGET: Final = 8000
MAX_GRAPH_LIMIT_PER_KIND: Final = 500
MAX_FANOUT: Final = 100
MAX_SEEDS: Final = 16


class ContextCompilerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ContextPathStep:
    edge_id: str
    from_node_id: str
    to_node_id: str
    relation_kind: str
    semantic_relation: str | None
    direction: str

    def internal_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CompiledContextItem:
    node_id: str
    kind: str
    source_type: str
    source_id: str
    label: str | None
    relevance_score: float
    hop_count: int
    matched_terms: tuple[str, ...]
    confidence: float | None
    valid_from: datetime | None
    valid_until: datetime | None
    path: tuple[ContextPathStep, ...]
    score_components: dict[str, float]
    attributes: dict[str, Any]
    provenance: dict[str, Any]

    def internal_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CompiledContextPacket:
    tenant_id: str
    query: str
    retrieval_method: str
    seed_node_ids: tuple[str, ...]
    items: tuple[CompiledContextItem, ...]
    graph_node_count: int
    graph_edge_count: int
    candidate_count: int
    estimated_tokens: int
    item_budget: int
    token_budget: int
    max_hops: int
    lexical_fallback_used: bool
    generated_at: datetime

    def internal_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Reachable:
    node_id: str
    seed_node_id: str
    seed_score: float
    matched_terms: tuple[str, ...]
    path: tuple[ContextPathStep, ...]

    @property
    def hops(self) -> int:
        return len(self.path)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return _utc(parsed)
    return None


def _node_secret(node: CognitiveNode) -> bool:
    if "sensitivity_class" in node.attributes:
        sensitivity = str(
            node.attributes.get("sensitivity_class") or ""
        ).strip().upper()
        if sensitivity not in {"NORMAL", "PRIVATE", "OWNER_PRIVATE"}:
            return True
    if "visibility" in node.attributes:
        visibility = str(node.attributes.get("visibility") or "").strip().upper()
        if visibility not in {"NORMAL", "PUBLIC", "PRIVATE", "OWNER_PRIVATE"}:
            return True
    return False


def _node_reference_time(node: CognitiveNode) -> datetime | None:
    for value in (
        node.attributes.get("last_activity_at"),
        node.provenance.get("last_observed_at"),
        node.provenance.get("observed_at"),
        node.valid_from,
    ):
        parsed = _parse_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _recency_weight(node: CognitiveNode, now: datetime) -> float:
    reference = _node_reference_time(node)
    if reference is None:
        return 0.6
    age_days = max(0.0, (now - reference).total_seconds() / 86400.0)
    return round(max(0.05, 1.0 / (1.0 + age_days / 30.0)), 6)


def _validity_weight(
    *,
    valid_from: datetime | None,
    valid_until: datetime | None,
    now: datetime,
) -> float:
    start = _utc(valid_from) if valid_from is not None else None
    end = _utc(valid_until) if valid_until is not None else None
    if end is not None and end <= now:
        age_days = max(0.0, (now - end).total_seconds() / 86400.0)
        return round(max(0.1, 0.5 / (1.0 + age_days / 30.0)), 6)
    if start is not None and start > now:
        distance_days = max(0.0, (start - now).total_seconds() / 86400.0)
        return round(max(0.2, 0.7 / (1.0 + distance_days / 30.0)), 6)
    return 1.0


def _provenance_weight(node: CognitiveNode) -> float:
    if node.source_type.endswith("_REFERENCE") or node.source_type == "ENTITY_REFERENCE":
        return 0.65
    if node.source_type == "MEMORY_CLAIM":
        return 0.85
    if node.source_type in {"TIMELINE_EVENT", "CONVERSATION_MESSAGE"}:
        return 0.9
    if node.source_type in {
        "ACTOR_IDENTITY",
        "RESOURCE",
        "RELATIONSHIP",
        "FACT",
        "ENTITY_STATE",
        "SEMANTIC_EPISODE",
    }:
        return 1.0
    return 0.75


def _node_confidence(node: CognitiveNode) -> float:
    if node.confidence is None:
        return 0.75
    return max(0.0, min(float(node.confidence), 1.0))


def _edge_path_weight(
    edge_index: dict[str, CognitiveEdge],
    path: tuple[ContextPathStep, ...],
    *,
    now: datetime,
) -> float:
    if not path:
        return 1.0
    weights: list[float] = []
    for step in path:
        edge = edge_index.get(step.edge_id)
        if edge is None:
            return 0.0
        confidence = (
            max(0.0, min(float(edge.confidence), 1.0))
            if edge.confidence is not None
            else 1.0
        )
        validity = _validity_weight(
            valid_from=edge.valid_from,
            valid_until=edge.valid_until,
            now=now,
        )
        weights.append(confidence * validity)
    return round(min(weights), 6)


def _node_search_text(node: CognitiveNode) -> tuple[str, str]:
    predicate = " ".join(
        part
        for part in (
            node.kind.value,
            node.source_type,
            node.label,
        )
        if part
    )
    searchable = " ".join(
        filter(
            None,
            (
                node.source_id,
                _json_text(node.attributes),
            ),
        )
    )
    return predicate, searchable


def _seed_candidates(
    graph: CognitiveGraphSlice,
    query: str,
    *,
    max_seeds: int,
) -> list[tuple[CognitiveNode, float, tuple[str, ...]]]:
    query_tokens = _tokens(query)
    if not query_tokens:
        return []

    seeds: list[tuple[CognitiveNode, float, tuple[str, ...]]] = []
    for node in graph.nodes:
        if _node_secret(node):
            continue
        predicate, searchable = _node_search_text(node)
        score, matched = _candidate_score(
            query_tokens=query_tokens,
            predicate=predicate,
            searchable_value=searchable,
            confidence=_node_confidence(node),
        )
        if score > 0:
            seeds.append((node, score, matched))

    seeds.sort(
        key=lambda item: (
            -item[1],
            -_node_confidence(item[0]),
            item[0].kind.value,
            item[0].node_id,
        )
    )
    return seeds[:max_seeds]


def _normalize_relation_filters(
    filters: Iterable[CognitiveRelationKind | str] | None,
) -> set[CognitiveRelationKind] | None:
    if filters is None:
        return None
    normalized: set[CognitiveRelationKind] = set()
    for value in filters:
        if isinstance(value, CognitiveRelationKind):
            normalized.add(value)
            continue
        try:
            normalized.add(CognitiveRelationKind(str(value).strip().upper()))
        except ValueError as exc:
            raise ContextCompilerError(
                "CONTEXT_COMPILER_RELATION_FILTER_INVALID"
            ) from exc
    if not normalized:
        raise ContextCompilerError("CONTEXT_COMPILER_RELATION_FILTER_EMPTY")
    return normalized


def _visible_subgraph(
    graph: CognitiveGraphSlice,
) -> tuple[dict[str, CognitiveNode], tuple[CognitiveEdge, ...]]:
    nodes = {
        node.node_id: node
        for node in graph.nodes
        if not _node_secret(node)
    }
    edges = tuple(
        edge
        for edge in graph.edges
        if edge.source_node_id in nodes and edge.target_node_id in nodes
    )
    return nodes, edges


def _expand(
    graph: CognitiveGraphSlice,
    seeds: list[tuple[CognitiveNode, float, tuple[str, ...]]],
    *,
    max_hops: int,
    max_fanout: int,
    relation_filters: set[CognitiveRelationKind] | None,
) -> dict[str, _Reachable]:
    nodes, visible_edges = _visible_subgraph(graph)
    adjacency: dict[
        str,
        list[tuple[str, CognitiveEdge, str]],
    ] = {node_id: [] for node_id in nodes}

    for edge in visible_edges:
        if relation_filters is not None and edge.relation_kind not in relation_filters:
            continue
        adjacency.setdefault(edge.source_node_id, []).append(
            (edge.target_node_id, edge, "OUTBOUND")
        )
        adjacency.setdefault(edge.target_node_id, []).append(
            (edge.source_node_id, edge, "INBOUND")
        )

    for node_id in adjacency:
        adjacency[node_id].sort(
            key=lambda item: (
                item[1].relation_kind.value,
                item[0],
                item[1].edge_id,
                item[2],
            )
        )

    reachable: dict[str, _Reachable] = {}
    queue: deque[_Reachable] = deque()
    for node, score, matched in seeds:
        if node.node_id not in nodes:
            continue
        candidate = _Reachable(
            node_id=node.node_id,
            seed_node_id=node.node_id,
            seed_score=score,
            matched_terms=matched,
            path=(),
        )
        current = reachable.get(node.node_id)
        if current is None or candidate.seed_score > current.seed_score:
            reachable[node.node_id] = candidate
            queue.append(candidate)

    while queue:
        current = queue.popleft()
        if current.hops >= max_hops:
            continue

        neighbors = adjacency.get(current.node_id, ())[:max_fanout]
        for neighbor_id, edge, direction in neighbors:
            step = ContextPathStep(
                edge_id=edge.edge_id,
                from_node_id=current.node_id,
                to_node_id=neighbor_id,
                relation_kind=edge.relation_kind.value,
                semantic_relation=edge.semantic_relation,
                direction=direction,
            )
            candidate = _Reachable(
                node_id=neighbor_id,
                seed_node_id=current.seed_node_id,
                seed_score=current.seed_score,
                matched_terms=current.matched_terms,
                path=current.path + (step,),
            )
            existing = reachable.get(neighbor_id)
            if existing is not None:
                existing_key = (
                    existing.hops,
                    -existing.seed_score,
                    existing.seed_node_id,
                    tuple(step.edge_id for step in existing.path),
                )
                candidate_key = (
                    candidate.hops,
                    -candidate.seed_score,
                    candidate.seed_node_id,
                    tuple(step.edge_id for step in candidate.path),
                )
                if existing_key <= candidate_key:
                    continue
            reachable[neighbor_id] = candidate
            queue.append(candidate)

    return reachable


def _score_node(
    *,
    edge_index: dict[str, CognitiveEdge],
    node: CognitiveNode,
    reachable: _Reachable,
    now: datetime,
) -> tuple[float, dict[str, float]]:
    lexical = max(0.0, min(reachable.seed_score, 1.0))
    hop = 1.0 / (1.0 + reachable.hops)
    confidence = _node_confidence(node)
    recency = _recency_weight(node, now)
    provenance = _provenance_weight(node)
    node_validity = _validity_weight(
        valid_from=node.valid_from,
        valid_until=node.valid_until,
        now=now,
    )
    path = _edge_path_weight(edge_index, reachable.path, now=now)

    score = (
        lexical * 0.32
        + hop * 0.20
        + confidence * 0.14
        + recency * 0.12
        + provenance * 0.08
        + node_validity * 0.08
        + path * 0.06
    )
    components = {
        "lexical_seed": round(lexical, 6),
        "hop": round(hop, 6),
        "confidence": round(confidence, 6),
        "recency": round(recency, 6),
        "provenance": round(provenance, 6),
        "node_validity": round(node_validity, 6),
        "path": round(path, 6),
    }
    return round(score, 6), components


def _estimate_tokens(payload: dict[str, Any]) -> int:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return max(1, math.ceil(len(raw) / 4))


def _bounded_items(
    candidates: list[CompiledContextItem],
    *,
    item_budget: int,
    token_budget: int,
) -> tuple[tuple[CompiledContextItem, ...], int]:
    selected: list[CompiledContextItem] = []
    used = 0
    for item in candidates:
        if len(selected) >= item_budget:
            break
        cost = _estimate_tokens(item.internal_payload())
        if used + cost > token_budget:
            continue
        selected.append(item)
        used += cost
    return tuple(selected), used


def _compile_lexical_fallback(
    session: Session,
    tenant_id: str,
    query: str,
    *,
    item_budget: int,
    token_budget: int,
    candidate_limit: int,
    generated_at: datetime,
    max_hops: int,
) -> CompiledContextPacket:
    result = retrieve_personal_context(
        session,
        tenant_id,
        query,
        limit=item_budget,
        candidate_limit=max(item_budget, candidate_limit),
    )
    candidates = [
        CompiledContextItem(
            node_id=f"lexical:{item.kind.lower()}:{item.item_id}",
            kind=item.kind,
            source_type=item.kind,
            source_id=item.item_id,
            label=item.predicate,
            relevance_score=item.relevance_score,
            hop_count=0,
            matched_terms=item.matched_terms,
            confidence=item.confidence,
            valid_from=None,
            valid_until=None,
            path=(),
            score_components={"lexical_v0": item.relevance_score},
            attributes={
                "predicate": item.predicate,
                "value_text": item.value_text,
                "value_json": item.value_json,
            },
            provenance={
                **item.provenance,
                "fallback": "LEXICAL_V0",
            },
        )
        for item in result.items
    ]
    items, used = _bounded_items(
        candidates,
        item_budget=item_budget,
        token_budget=token_budget,
    )
    return CompiledContextPacket(
        tenant_id=tenant_id,
        query=query,
        retrieval_method="LEXICAL_FALLBACK_V0",
        seed_node_ids=(),
        items=items,
        graph_node_count=0,
        graph_edge_count=0,
        candidate_count=result.candidate_count,
        estimated_tokens=used,
        item_budget=item_budget,
        token_budget=token_budget,
        max_hops=max_hops,
        lexical_fallback_used=True,
        generated_at=generated_at,
    )


def compile_graph_context(
    session: Session,
    tenant_id: str,
    query: str,
    *,
    max_hops: int = 2,
    item_budget: int = 12,
    token_budget: int = 1800,
    max_fanout: int = 30,
    max_seeds: int = 8,
    graph_limit_per_kind: int = 200,
    lexical_candidate_limit: int = 200,
    relation_filters: Iterable[CognitiveRelationKind | str] | None = None,
    now: datetime | None = None,
) -> CompiledContextPacket:
    """Compile bounded graph context, falling back to lexical V0 without authority."""
    if max_hops < 0 or max_hops > MAX_HOPS:
        raise ContextCompilerError("CONTEXT_COMPILER_HOPS_OUT_OF_RANGE")
    if item_budget < 1 or item_budget > MAX_ITEM_BUDGET:
        raise ContextCompilerError("CONTEXT_COMPILER_ITEM_BUDGET_OUT_OF_RANGE")
    if token_budget < 32 or token_budget > MAX_TOKEN_BUDGET:
        raise ContextCompilerError("CONTEXT_COMPILER_TOKEN_BUDGET_OUT_OF_RANGE")
    if max_fanout < 1 or max_fanout > MAX_FANOUT:
        raise ContextCompilerError("CONTEXT_COMPILER_FANOUT_OUT_OF_RANGE")
    if max_seeds < 1 or max_seeds > MAX_SEEDS:
        raise ContextCompilerError("CONTEXT_COMPILER_SEED_LIMIT_OUT_OF_RANGE")
    if graph_limit_per_kind < 1 or graph_limit_per_kind > MAX_GRAPH_LIMIT_PER_KIND:
        raise ContextCompilerError("CONTEXT_COMPILER_GRAPH_LIMIT_OUT_OF_RANGE")
    if lexical_candidate_limit < item_budget or lexical_candidate_limit > 500:
        raise ContextCompilerError(
            "CONTEXT_COMPILER_LEXICAL_CANDIDATE_LIMIT_OUT_OF_RANGE"
        )
    if len(query) > 4000:
        raise ContextCompilerError("CONTEXT_COMPILER_QUERY_TOO_LONG")

    stamp = _utc(now or datetime.now(UTC))
    normalized_filters = _normalize_relation_filters(relation_filters)

    graph = build_cognitive_graph_slice(
        session,
        tenant_id,
        limit_per_kind=graph_limit_per_kind,
        include_secret=False,
        now=stamp,
    )
    visible_nodes, visible_edges = _visible_subgraph(graph)
    seeds = _seed_candidates(graph, query, max_seeds=max_seeds)
    if not seeds:
        return _compile_lexical_fallback(
            session,
            tenant_id,
            query,
            item_budget=item_budget,
            token_budget=token_budget,
            candidate_limit=lexical_candidate_limit,
            generated_at=stamp,
            max_hops=max_hops,
        )

    reachable = _expand(
        graph,
        seeds,
        max_hops=max_hops,
        max_fanout=max_fanout,
        relation_filters=normalized_filters,
    )
    edge_index = {edge.edge_id: edge for edge in graph.edges}
    candidates: list[CompiledContextItem] = []
    for node_id, reach in reachable.items():
        node = visible_nodes.get(node_id)
        if node is None:
            continue
        score, components = _score_node(
            edge_index=edge_index,
            node=node,
            reachable=reach,
            now=stamp,
        )
        candidates.append(
            CompiledContextItem(
                node_id=node.node_id,
                kind=node.kind.value,
                source_type=node.source_type,
                source_id=node.source_id,
                label=node.label,
                relevance_score=score,
                hop_count=reach.hops,
                matched_terms=reach.matched_terms,
                confidence=node.confidence,
                valid_from=node.valid_from,
                valid_until=node.valid_until,
                path=reach.path,
                score_components=components,
                attributes=node.attributes,
                provenance={
                    **node.provenance,
                    "seed_node_id": reach.seed_node_id,
                    "selection": "GRAPH_AWARE_V1",
                    "grants_authority": False,
                },
            )
        )

    candidates.sort(
        key=lambda item: (
            -item.relevance_score,
            item.hop_count,
            item.kind,
            item.node_id,
        )
    )
    items, used = _bounded_items(
        candidates,
        item_budget=item_budget,
        token_budget=token_budget,
    )
    return CompiledContextPacket(
        tenant_id=tenant_id,
        query=query,
        retrieval_method="GRAPH_AWARE_V1",
        seed_node_ids=tuple(node.node_id for node, _, _ in seeds),
        items=items,
        graph_node_count=len(visible_nodes),
        graph_edge_count=len(visible_edges),
        candidate_count=len(candidates),
        estimated_tokens=used,
        item_budget=item_budget,
        token_budget=token_budget,
        max_hops=max_hops,
        lexical_fallback_used=False,
        generated_at=stamp,
    )


__all__ = [
    "CompiledContextItem",
    "CompiledContextPacket",
    "ContextCompilerError",
    "ContextPathStep",
    "compile_graph_context",
]
