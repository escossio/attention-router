"""Deterministic, explainable Graph Intelligence V0.

V2I operates over a tenant-scoped CognitiveGraphSlice.  Heuristic outputs are
bounded and never mutate canonical graph relationships.  Relation candidates
may cross the existing V2E CandidateInsight boundary when they have supported
raw evidence.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict
from datetime import UTC, datetime
from itertools import combinations
from typing import Iterable

from sqlalchemy.orm import Session

from attention_router.application.candidate_insights import (
    CandidateEvidenceInput,
    propose_candidate_insight,
)
from attention_router.domain.cognitive_graph import (
    CognitiveEdge,
    CognitiveGraphSlice,
    CognitiveInferenceClass,
    CognitiveNode,
    CognitiveNodeKind,
    CognitiveRelationKind,
)
from attention_router.domain.graph_intelligence import (
    GraphActivationResult,
    GraphAnomalyFeatures,
    GraphIntelligenceBudget,
    GraphIntelligenceEngines,
    GraphPathStep,
    GraphRankedPath,
    GraphRelationCandidate,
    GraphSimilarityResult,
)
from attention_router.infrastructure.candidate_insight_models import (
    CandidateInsightRow,
)


MAX_HOPS = 4
MAX_FANOUT = 100
MAX_NODES = 500
MAX_PATHS = 32
MAX_CANDIDATES = 64
ACTIVATION_DECAY = 0.62
MIN_PROPAGATED_ACTIVATION = 0.01
ENGINE_VERSION = "V0"


class GraphIntelligenceError(RuntimeError):
    pass


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _budget(
    value: GraphIntelligenceBudget | None,
) -> GraphIntelligenceBudget:
    current = value or GraphIntelligenceBudget()
    if current.max_hops < 0 or current.max_hops > MAX_HOPS:
        raise GraphIntelligenceError("GRAPH_INTELLIGENCE_HOPS_OUT_OF_RANGE")
    if current.max_fanout < 1 or current.max_fanout > MAX_FANOUT:
        raise GraphIntelligenceError("GRAPH_INTELLIGENCE_FANOUT_OUT_OF_RANGE")
    if current.max_nodes < 1 or current.max_nodes > MAX_NODES:
        raise GraphIntelligenceError("GRAPH_INTELLIGENCE_NODES_OUT_OF_RANGE")
    if current.max_paths < 1 or current.max_paths > MAX_PATHS:
        raise GraphIntelligenceError("GRAPH_INTELLIGENCE_PATHS_OUT_OF_RANGE")
    if current.max_candidates < 1 or current.max_candidates > MAX_CANDIDATES:
        raise GraphIntelligenceError(
            "GRAPH_INTELLIGENCE_CANDIDATES_OUT_OF_RANGE"
        )
    return current


def _node_visible(node: CognitiveNode) -> bool:
    for key in ("sensitivity_class", "visibility"):
        if key not in node.attributes:
            continue
        value = str(node.attributes.get(key) or "").strip().upper()
        allowed = {
            "sensitivity_class": {"NORMAL", "PRIVATE", "OWNER_PRIVATE"},
            "visibility": {"NORMAL", "PUBLIC", "PRIVATE", "OWNER_PRIVATE"},
        }[key]
        if value not in allowed:
            return False
    return True


def _edge_active(edge: CognitiveEdge, *, now: datetime) -> bool:
    start = _utc(edge.valid_from)
    end = _utc(edge.valid_until)
    if start is not None and start > now:
        return False
    if end is not None and end <= now:
        return False
    return True


def _edge_weight(edge: CognitiveEdge) -> float:
    confidence = (
        max(0.0, min(float(edge.confidence), 1.0))
        if edge.confidence is not None
        else 1.0
    )
    inference = (
        1.0
        if edge.inference_class is CognitiveInferenceClass.EXPLICIT
        else 0.90
    )
    return round(confidence * inference, 6)


def _visible_graph(
    graph: CognitiveGraphSlice,
) -> tuple[dict[str, CognitiveNode], tuple[CognitiveEdge, ...]]:
    graph.validate()
    nodes = {
        node.node_id: node
        for node in graph.nodes
        if _node_visible(node)
    }
    edges = tuple(
        edge
        for edge in graph.edges
        if edge.source_node_id in nodes and edge.target_node_id in nodes
    )
    return nodes, edges


def _active_edges(
    graph: CognitiveGraphSlice,
    edges: Iterable[CognitiveEdge],
) -> tuple[CognitiveEdge, ...]:
    now = _utc(graph.generated_at)
    assert now is not None
    return tuple(edge for edge in edges if _edge_active(edge, now=now))


def _step(
    *,
    edge: CognitiveEdge,
    from_node_id: str,
    to_node_id: str,
) -> GraphPathStep:
    if edge.source_node_id == from_node_id:
        direction = "OUTBOUND"
    elif edge.target_node_id == from_node_id:
        direction = "INBOUND"
    else:
        raise GraphIntelligenceError("GRAPH_INTELLIGENCE_PATH_EDGE_MISMATCH")
    return GraphPathStep(
        edge_id=edge.edge_id,
        from_node_id=from_node_id,
        to_node_id=to_node_id,
        relation_kind=edge.relation_kind.value,
        semantic_relation=edge.semantic_relation,
        direction=direction,
        edge_weight=_edge_weight(edge),
    )


def _adjacency(
    nodes: dict[str, CognitiveNode],
    edges: Iterable[CognitiveEdge],
) -> dict[str, tuple[tuple[str, CognitiveEdge], ...]]:
    items: dict[str, list[tuple[str, CognitiveEdge]]] = {
        node_id: [] for node_id in nodes
    }
    for edge in edges:
        items[edge.source_node_id].append((edge.target_node_id, edge))
        items[edge.target_node_id].append((edge.source_node_id, edge))
    return {
        node_id: tuple(
            sorted(
                neighbors,
                key=lambda item: (
                    item[1].relation_kind.value,
                    item[1].semantic_relation or "",
                    item[0],
                    item[1].edge_id,
                ),
            )
        )
        for node_id, neighbors in items.items()
    }


def _validated_seeds(
    nodes: dict[str, CognitiveNode],
    seeds: dict[str, float],
    *,
    max_nodes: int,
) -> dict[str, float]:
    if not seeds:
        raise GraphIntelligenceError("GRAPH_INTELLIGENCE_SEED_REQUIRED")
    if len(seeds) > max_nodes:
        raise GraphIntelligenceError("GRAPH_INTELLIGENCE_SEEDS_OUT_OF_RANGE")
    normalized: dict[str, float] = {}
    for node_id, score in sorted(seeds.items()):
        if node_id not in nodes:
            raise GraphIntelligenceError(
                "GRAPH_INTELLIGENCE_SEED_NOT_VISIBLE"
            )
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or score <= 0
            or score > 1
        ):
            raise GraphIntelligenceError(
                "GRAPH_INTELLIGENCE_SEED_SCORE_INVALID"
            )
        normalized[node_id] = float(score)
    return normalized


class DeterministicSpreadingActivationEngine:
    engine_name = "DETERMINISTIC_SPREADING_ACTIVATION"
    engine_version = ENGINE_VERSION

    def activate(
        self,
        graph: CognitiveGraphSlice,
        *,
        seeds: dict[str, float],
        budget: GraphIntelligenceBudget | None = None,
    ) -> tuple[GraphActivationResult, ...]:
        limits = _budget(budget)
        nodes, visible_edges = _visible_graph(graph)
        edges = _active_edges(graph, visible_edges)
        adjacency = _adjacency(nodes, edges)
        normalized = _validated_seeds(
            nodes,
            seeds,
            max_nodes=limits.max_nodes,
        )

        total: dict[str, float] = dict(normalized)
        best_path: dict[str, tuple[GraphPathStep, ...]] = {
            node_id: () for node_id in normalized
        }
        best_seed: dict[str, str] = {
            node_id: node_id for node_id in normalized
        }
        frontier: dict[
            str,
            tuple[float, str, tuple[GraphPathStep, ...]],
        ] = {
            node_id: (score, node_id, ())
            for node_id, score in normalized.items()
        }

        for _hop in range(1, limits.max_hops + 1):
            next_strength: dict[str, float] = defaultdict(float)
            next_best: dict[
                str,
                tuple[float, str, tuple[GraphPathStep, ...]],
            ] = {}

            for node_id in sorted(frontier):
                strength, seed_id, path = frontier[node_id]
                visited = {
                    step.from_node_id for step in path
                } | {node_id}
                neighbors = adjacency.get(node_id, ())[: limits.max_fanout]

                for neighbor_id, edge in neighbors:
                    if neighbor_id in visited:
                        continue
                    contribution = (
                        strength
                        * ACTIVATION_DECAY
                        * _edge_weight(edge)
                    )
                    if contribution < MIN_PROPAGATED_ACTIVATION:
                        continue
                    step = _step(
                        edge=edge,
                        from_node_id=node_id,
                        to_node_id=neighbor_id,
                    )
                    candidate_path = path + (step,)
                    next_strength[neighbor_id] = min(
                        1.0,
                        next_strength[neighbor_id] + contribution,
                    )
                    current = next_best.get(neighbor_id)
                    key = (
                        -contribution,
                        seed_id,
                        tuple(item.edge_id for item in candidate_path),
                    )
                    if current is None:
                        next_best[neighbor_id] = (
                            contribution,
                            seed_id,
                            candidate_path,
                        )
                    else:
                        current_key = (
                            -current[0],
                            current[1],
                            tuple(item.edge_id for item in current[2]),
                        )
                        if key < current_key:
                            next_best[neighbor_id] = (
                                contribution,
                                seed_id,
                                candidate_path,
                            )

            if not next_strength:
                break

            frontier = {}
            ranked_next = sorted(
                next_strength,
                key=lambda node_id: (
                    -next_strength[node_id],
                    node_id,
                ),
            )
            for node_id in ranked_next:
                if node_id not in total and len(total) >= limits.max_nodes:
                    continue
                contribution = min(1.0, next_strength[node_id])
                total[node_id] = min(
                    1.0,
                    total.get(node_id, 0.0) + contribution,
                )
                best = next_best[node_id]
                prior_path = best_path.get(node_id)
                if (
                    prior_path is None
                    or len(best[2]) < len(prior_path)
                    or (
                        len(best[2]) == len(prior_path)
                        and tuple(step.edge_id for step in best[2])
                        < tuple(step.edge_id for step in prior_path)
                    )
                ):
                    best_path[node_id] = best[2]
                    best_seed[node_id] = best[1]
                frontier[node_id] = (
                    contribution,
                    best[1],
                    best[2],
                )

            if not frontier:
                break

        results = [
            GraphActivationResult(
                node_id=node_id,
                activation=round(score, 6),
                seed_node_id=best_seed[node_id],
                hop_count=len(best_path[node_id]),
                path=best_path[node_id],
            )
            for node_id, score in total.items()
        ]
        results.sort(
            key=lambda item: (
                -item.activation,
                item.hop_count,
                item.node_id,
            )
        )
        return tuple(results[: limits.max_nodes])


def _neighbor_signatures(
    *,
    node_id: str,
    nodes: dict[str, CognitiveNode],
    adjacency: dict[str, tuple[tuple[str, CognitiveEdge], ...]],
    max_fanout: int,
) -> set[str]:
    signatures: set[str] = set()
    for neighbor_id, edge in adjacency.get(node_id, ())[:max_fanout]:
        direction = (
            "OUT"
            if edge.source_node_id == node_id
            else "IN"
        )
        signatures.add(
            "|".join(
                [
                    direction,
                    edge.relation_kind.value,
                    edge.semantic_relation or "",
                    nodes[neighbor_id].kind.value,
                ]
            )
        )
    return signatures


class DeterministicStructuralSimilarityEngine:
    engine_name = "DETERMINISTIC_STRUCTURAL_SIMILARITY"
    engine_version = ENGINE_VERSION

    def similar_nodes(
        self,
        graph: CognitiveGraphSlice,
        *,
        node_id: str,
        budget: GraphIntelligenceBudget | None = None,
    ) -> tuple[GraphSimilarityResult, ...]:
        limits = _budget(budget)
        nodes, visible_edges = _visible_graph(graph)
        edges = _active_edges(graph, visible_edges)
        if node_id not in nodes:
            raise GraphIntelligenceError(
                "GRAPH_INTELLIGENCE_NODE_NOT_VISIBLE"
            )
        adjacency = _adjacency(nodes, edges)
        source = nodes[node_id]
        source_signatures = _neighbor_signatures(
            node_id=node_id,
            nodes=nodes,
            adjacency=adjacency,
            max_fanout=limits.max_fanout,
        )

        results: list[GraphSimilarityResult] = []
        candidates = [
            item
            for item in nodes.values()
            if item.node_id != node_id and item.kind is source.kind
        ]
        candidates.sort(key=lambda item: item.node_id)

        for candidate in candidates[: limits.max_nodes]:
            signatures = _neighbor_signatures(
                node_id=candidate.node_id,
                nodes=nodes,
                adjacency=adjacency,
                max_fanout=limits.max_fanout,
            )
            union = source_signatures | signatures
            if not union:
                continue
            shared = source_signatures & signatures
            similarity = len(shared) / len(union)
            if similarity <= 0:
                continue
            results.append(
                GraphSimilarityResult(
                    node_id=node_id,
                    compared_node_id=candidate.node_id,
                    similarity=round(similarity, 6),
                    shared_signature_count=len(shared),
                    union_signature_count=len(union),
                    shared_signatures=tuple(sorted(shared)),
                )
            )

        results.sort(
            key=lambda item: (
                -item.similarity,
                item.compared_node_id,
            )
        )
        return tuple(results[: limits.max_candidates])


class DeterministicContextPathRanker:
    engine_name = "DETERMINISTIC_CONTEXT_PATH_RANKER"
    engine_version = ENGINE_VERSION

    def rank_paths(
        self,
        graph: CognitiveGraphSlice,
        *,
        source_node_id: str,
        target_node_id: str,
        budget: GraphIntelligenceBudget | None = None,
    ) -> tuple[GraphRankedPath, ...]:
        limits = _budget(budget)
        nodes, visible_edges = _visible_graph(graph)
        edges = _active_edges(graph, visible_edges)
        if source_node_id not in nodes or target_node_id not in nodes:
            raise GraphIntelligenceError(
                "GRAPH_INTELLIGENCE_PATH_NODE_NOT_VISIBLE"
            )
        if source_node_id == target_node_id:
            return ()

        adjacency = _adjacency(nodes, edges)
        queue = deque(
            [(source_node_id, tuple(), frozenset({source_node_id}), 1.0)]
        )
        results: list[GraphRankedPath] = []
        expansions = 0

        while queue and len(results) < limits.max_paths:
            node_id, path, visited, score = queue.popleft()
            if len(path) >= limits.max_hops:
                continue

            for neighbor_id, edge in adjacency.get(
                node_id,
                (),
            )[: limits.max_fanout]:
                if neighbor_id in visited:
                    continue
                expansions += 1
                if expansions > limits.max_nodes:
                    queue.clear()
                    break

                step = _step(
                    edge=edge,
                    from_node_id=node_id,
                    to_node_id=neighbor_id,
                )
                candidate_path = path + (step,)
                candidate_score = (
                    score * step.edge_weight * 0.90
                )
                if neighbor_id == target_node_id:
                    results.append(
                        GraphRankedPath(
                            source_node_id=source_node_id,
                            target_node_id=target_node_id,
                            score=round(candidate_score, 6),
                            hops=len(candidate_path),
                            steps=candidate_path,
                        )
                    )
                    if len(results) >= limits.max_paths:
                        break
                    continue

                queue.append(
                    (
                        neighbor_id,
                        candidate_path,
                        visited | {neighbor_id},
                        candidate_score,
                    )
                )

        results.sort(
            key=lambda item: (
                -item.score,
                item.hops,
                tuple(step.edge_id for step in item.steps),
            )
        )
        return tuple(results[: limits.max_paths])


class DeterministicAnomalyFeatureEngine:
    engine_name = "DETERMINISTIC_GRAPH_ANOMALY_FEATURES"
    engine_version = ENGINE_VERSION

    def features(
        self,
        graph: CognitiveGraphSlice,
        *,
        node_id: str,
        budget: GraphIntelligenceBudget | None = None,
    ) -> GraphAnomalyFeatures:
        limits = _budget(budget)
        nodes, visible_edges = _visible_graph(graph)
        if node_id not in nodes:
            raise GraphIntelligenceError(
                "GRAPH_INTELLIGENCE_NODE_NOT_VISIBLE"
            )
        incident = [
            edge
            for edge in visible_edges
            if edge.source_node_id == node_id
            or edge.target_node_id == node_id
        ][: limits.max_fanout]

        now = _utc(graph.generated_at)
        assert now is not None
        relation_kinds = {edge.relation_kind.value for edge in incident}
        neighbor_kinds: set[str] = set()
        low_confidence = 0
        expired = 0

        for edge in incident:
            neighbor_id = (
                edge.target_node_id
                if edge.source_node_id == node_id
                else edge.source_node_id
            )
            neighbor_kinds.add(nodes[neighbor_id].kind.value)
            confidence = (
                float(edge.confidence)
                if edge.confidence is not None
                else 1.0
            )
            if confidence < 0.5:
                low_confidence += 1
            end = _utc(edge.valid_until)
            if end is not None and end <= now:
                expired += 1

        degree = len(incident)
        denominator = max(1, degree)
        return GraphAnomalyFeatures(
            node_id=node_id,
            degree=degree,
            relation_kind_count=len(relation_kinds),
            neighbor_kind_count=len(neighbor_kinds),
            low_confidence_edge_ratio=round(
                low_confidence / denominator,
                6,
            ),
            expired_edge_ratio=round(expired / denominator, 6),
            isolated=degree == 0,
        )


def _explicit_pair_keys(
    edges: Iterable[CognitiveEdge],
) -> set[frozenset[str]]:
    return {
        frozenset({edge.source_node_id, edge.target_node_id})
        for edge in edges
        if edge.relation_kind is CognitiveRelationKind.EXPLICIT_RELATION
    }


def _candidate_pair(
    first: CognitiveNode,
    second: CognitiveNode,
) -> tuple[CognitiveNode, CognitiveNode] | None:
    kinds = {first.kind, second.kind}
    if kinds != {
        CognitiveNodeKind.PERSON,
        CognitiveNodeKind.RESOURCE,
    }:
        return None
    if first.kind is CognitiveNodeKind.PERSON:
        return first, second
    return second, first


class DeterministicRelationCandidateEngine:
    engine_name = "DETERMINISTIC_RELATION_CANDIDATE"
    engine_version = ENGINE_VERSION

    def __init__(
        self,
        path_ranker: DeterministicContextPathRanker | None = None,
    ) -> None:
        self._path_ranker = (
            path_ranker or DeterministicContextPathRanker()
        )

    def candidates(
        self,
        graph: CognitiveGraphSlice,
        *,
        budget: GraphIntelligenceBudget | None = None,
    ) -> tuple[GraphRelationCandidate, ...]:
        limits = _budget(budget)
        nodes, visible_edges = _visible_graph(graph)
        edges = _active_edges(graph, visible_edges)
        explicit_pairs = _explicit_pair_keys(edges)

        attachments: dict[str, set[str]] = defaultdict(set)
        allowed_relations = {
            CognitiveRelationKind.EVENT_ACTOR,
            CognitiveRelationKind.EVENT_RESOURCE,
            CognitiveRelationKind.EVENT_RELATIONSHIP,
        }
        event_candidates = sorted(
            (
                node
                for node in nodes.values()
                if (
                    node.kind is CognitiveNodeKind.EVENT
                    and node.source_type == "TIMELINE_EVENT"
                )
            ),
            key=lambda node: node.node_id,
        )[: limits.max_nodes]
        event_nodes = {
            node.node_id: node
            for node in event_candidates
        }

        for edge in edges:
            if edge.relation_kind not in allowed_relations:
                continue
            if edge.source_node_id in event_nodes:
                event_id = edge.source_node_id
                other = edge.target_node_id
            elif edge.target_node_id in event_nodes:
                event_id = edge.target_node_id
                other = edge.source_node_id
            else:
                continue
            if other in nodes:
                attachments[event_id].add(other)

        support: dict[
            tuple[str, str],
            set[str],
        ] = defaultdict(set)
        for event_node_id in sorted(attachments):
            attached = sorted(attachments[event_node_id])
            for left_id, right_id in combinations(attached, 2):
                pair = _candidate_pair(nodes[left_id], nodes[right_id])
                if pair is None:
                    continue
                subject, target = pair
                pair_key = frozenset(
                    {subject.node_id, target.node_id}
                )
                if pair_key in explicit_pairs:
                    continue
                support[(subject.node_id, target.node_id)].add(
                    event_node_id
                )

        results: list[GraphRelationCandidate] = []
        for (subject_id, target_id), event_node_ids in sorted(
            support.items()
        ):
            if len(event_node_ids) < 2:
                continue
            event_source_ids = tuple(
                sorted(
                    event_nodes[node_id].source_id
                    for node_id in event_node_ids
                )
            )
            confidence = min(
                0.95,
                0.55 + 0.10 * len(event_source_ids),
            )
            paths = self._path_ranker.rank_paths(
                graph,
                source_node_id=subject_id,
                target_node_id=target_id,
                budget=GraphIntelligenceBudget(
                    max_hops=min(2, limits.max_hops),
                    max_fanout=limits.max_fanout,
                    max_nodes=limits.max_nodes,
                    max_paths=min(
                        len(event_source_ids),
                        limits.max_paths,
                    ),
                    max_candidates=limits.max_candidates,
                ),
            )
            results.append(
                GraphRelationCandidate(
                    subject_node_id=subject_id,
                    target_node_id=target_id,
                    relation_hint="REPEATED_EVENT_COOCCURRENCE",
                    confidence=round(confidence, 6),
                    support_event_ids=event_source_ids,
                    explanation_paths=paths,
                    provenance={
                        "engine": self.engine_name,
                        "engine_version": self.engine_version,
                        "support_event_count": len(event_source_ids),
                        "grants_authority": False,
                        "materializes_canonical_truth": False,
                    },
                )
            )

        results.sort(
            key=lambda item: (
                -item.confidence,
                item.subject_node_id,
                item.target_node_id,
            )
        )
        return tuple(results[: limits.max_candidates])


def _candidate_entity_type(node: CognitiveNode) -> str:
    if node.kind is CognitiveNodeKind.PERSON:
        return "PERSON"
    if node.kind is CognitiveNodeKind.RESOURCE:
        return "RESOURCE"
    if node.kind is CognitiveNodeKind.RELATIONSHIP:
        return "RELATIONSHIP"
    raise GraphIntelligenceError(
        "GRAPH_INTELLIGENCE_CANDIDATE_NODE_TYPE_UNSUPPORTED"
    )


def persist_relation_candidate_insight(
    session: Session,
    *,
    graph: CognitiveGraphSlice,
    candidate: GraphRelationCandidate,
    now: datetime | None = None,
) -> tuple[CandidateInsightRow, bool]:
    """Cross one deterministic graph link candidate into V2E governance."""
    nodes, _edges = _visible_graph(graph)
    subject = nodes.get(candidate.subject_node_id)
    target = nodes.get(candidate.target_node_id)
    if subject is None or target is None:
        raise GraphIntelligenceError(
            "GRAPH_INTELLIGENCE_CANDIDATE_NODE_NOT_VISIBLE"
        )
    if subject.tenant_id != graph.tenant_id or target.tenant_id != graph.tenant_id:
        raise GraphIntelligenceError(
            "GRAPH_INTELLIGENCE_CANDIDATE_TENANT_MISMATCH"
        )
    if candidate.relation_hint != "REPEATED_EVENT_COOCCURRENCE":
        raise GraphIntelligenceError(
            "GRAPH_INTELLIGENCE_RELATION_HINT_UNSUPPORTED"
        )
    if len(candidate.support_event_ids) < 2:
        raise GraphIntelligenceError(
            "GRAPH_INTELLIGENCE_RELATION_SUPPORT_INSUFFICIENT"
        )

    visible_candidates = DeterministicRelationCandidateEngine().candidates(
        graph,
        budget=GraphIntelligenceBudget(
            max_hops=2,
            max_fanout=MAX_FANOUT,
            max_nodes=MAX_NODES,
            max_paths=MAX_PATHS,
            max_candidates=MAX_CANDIDATES,
        ),
    )
    canonical = next(
        (
            item
            for item in visible_candidates
            if (
                item.subject_node_id == candidate.subject_node_id
                and item.target_node_id == candidate.target_node_id
                and item.relation_hint == candidate.relation_hint
            )
        ),
        None,
    )
    if canonical is None:
        raise GraphIntelligenceError(
            "GRAPH_INTELLIGENCE_RELATION_CANDIDATE_STALE"
        )
    if (
        canonical.support_event_ids != candidate.support_event_ids
        or canonical.confidence != candidate.confidence
    ):
        raise GraphIntelligenceError(
            "GRAPH_INTELLIGENCE_RELATION_CANDIDATE_MISMATCH"
        )

    evidence = [
        CandidateEvidenceInput(
            evidence_type="TIMELINE_EVENT",
            source_ref=event_id,
            independence_key=f"graph-event:{event_id}",
            confidence=canonical.confidence,
            provenance={
                "graph_relation_hint": canonical.relation_hint,
                "graph_engine": canonical.provenance.get("engine"),
                "graph_engine_version": canonical.provenance.get(
                    "engine_version"
                ),
            },
        )
        for event_id in canonical.support_event_ids
    ]
    path_payload = [
        {
            "score": path.score,
            "hops": path.hops,
            "steps": [asdict(step) for step in path.steps],
        }
        for path in canonical.explanation_paths
    ]

    return propose_candidate_insight(
        session,
        tenant_id=graph.tenant_id,
        insight_type="RELATIONSHIP_PROPOSAL",
        subject_type=_candidate_entity_type(subject),
        subject_id=subject.source_id,
        predicate="context.graph.structural_association",
        proposed_value={
            "target_type": _candidate_entity_type(target),
            "target_id": target.source_id,
            "relation_hint": canonical.relation_hint,
            "support_event_count": len(canonical.support_event_ids),
        },
        source_engine="RULE",
        confidence=canonical.confidence,
        evidence=evidence,
        provenance={
            **canonical.provenance,
            "graph_subject_node_id": subject.node_id,
            "graph_target_node_id": target.node_id,
            "explanation_paths": path_payload,
            "governance": "CANDIDATE_ONLY",
        },
        now=now,
    )


def default_graph_intelligence_engines() -> GraphIntelligenceEngines:
    path_ranker = DeterministicContextPathRanker()
    return GraphIntelligenceEngines(
        relevance=DeterministicSpreadingActivationEngine(),
        similarity=DeterministicStructuralSimilarityEngine(),
        relation_candidates=DeterministicRelationCandidateEngine(
            path_ranker
        ),
        anomaly=DeterministicAnomalyFeatureEngine(),
        path_ranker=path_ranker,
    )


__all__ = [
    "DeterministicAnomalyFeatureEngine",
    "DeterministicContextPathRanker",
    "DeterministicRelationCandidateEngine",
    "DeterministicSpreadingActivationEngine",
    "DeterministicStructuralSimilarityEngine",
    "GraphIntelligenceError",
    "default_graph_intelligence_engines",
    "persist_relation_candidate_insight",
]
