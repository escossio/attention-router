"""Stable semantic contracts for Personal Context V2I Graph Intelligence.

Engines operate over the explicit Cognitive Graph and return bounded,
explainable scores/candidates.  They do not mutate canonical graph knowledge
or grant execution/disclosure authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from attention_router.domain.cognitive_graph import CognitiveGraphSlice


@dataclass(frozen=True, slots=True)
class GraphIntelligenceBudget:
    max_hops: int = 3
    max_fanout: int = 40
    max_nodes: int = 300
    max_paths: int = 12
    max_candidates: int = 24


@dataclass(frozen=True, slots=True)
class GraphPathStep:
    edge_id: str
    from_node_id: str
    to_node_id: str
    relation_kind: str
    semantic_relation: str | None
    direction: str
    edge_weight: float


@dataclass(frozen=True, slots=True)
class GraphActivationResult:
    node_id: str
    activation: float
    seed_node_id: str
    hop_count: int
    path: tuple[GraphPathStep, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphSimilarityResult:
    node_id: str
    compared_node_id: str
    similarity: float
    shared_signature_count: int
    union_signature_count: int
    shared_signatures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphRankedPath:
    source_node_id: str
    target_node_id: str
    score: float
    hops: int
    steps: tuple[GraphPathStep, ...]


@dataclass(frozen=True, slots=True)
class GraphRelationCandidate:
    subject_node_id: str
    target_node_id: str
    relation_hint: str
    confidence: float
    support_event_ids: tuple[str, ...]
    explanation_paths: tuple[GraphRankedPath, ...] = ()
    provenance: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GraphAnomalyFeatures:
    node_id: str
    degree: int
    relation_kind_count: int
    neighbor_kind_count: int
    low_confidence_edge_ratio: float
    expired_edge_ratio: float
    isolated: bool
    feature_version: str = "GRAPH_ANOMALY_FEATURES_V0"


class RelevanceEngine(Protocol):
    engine_name: str
    engine_version: str

    def activate(
        self,
        graph: CognitiveGraphSlice,
        *,
        seeds: dict[str, float],
        budget: GraphIntelligenceBudget | None = None,
    ) -> tuple[GraphActivationResult, ...]:
        ...


class StructuralSimilarityEngine(Protocol):
    engine_name: str
    engine_version: str

    def similar_nodes(
        self,
        graph: CognitiveGraphSlice,
        *,
        node_id: str,
        budget: GraphIntelligenceBudget | None = None,
    ) -> tuple[GraphSimilarityResult, ...]:
        ...


class RelationCandidateEngine(Protocol):
    engine_name: str
    engine_version: str

    def candidates(
        self,
        graph: CognitiveGraphSlice,
        *,
        budget: GraphIntelligenceBudget | None = None,
    ) -> tuple[GraphRelationCandidate, ...]:
        ...


class AnomalyEngine(Protocol):
    engine_name: str
    engine_version: str

    def features(
        self,
        graph: CognitiveGraphSlice,
        *,
        node_id: str,
        budget: GraphIntelligenceBudget | None = None,
    ) -> GraphAnomalyFeatures:
        ...


class ContextPathRanker(Protocol):
    engine_name: str
    engine_version: str

    def rank_paths(
        self,
        graph: CognitiveGraphSlice,
        *,
        source_node_id: str,
        target_node_id: str,
        budget: GraphIntelligenceBudget | None = None,
    ) -> tuple[GraphRankedPath, ...]:
        ...


@dataclass(frozen=True, slots=True)
class GraphIntelligenceEngines:
    relevance: RelevanceEngine
    similarity: StructuralSimilarityEngine
    relation_candidates: RelationCandidateEngine
    anomaly: AnomalyEngine
    path_ranker: ContextPathRanker


__all__ = [
    "AnomalyEngine",
    "ContextPathRanker",
    "GraphActivationResult",
    "GraphAnomalyFeatures",
    "GraphIntelligenceBudget",
    "GraphIntelligenceEngines",
    "GraphPathStep",
    "GraphRankedPath",
    "GraphRelationCandidate",
    "GraphSimilarityResult",
    "RelationCandidateEngine",
    "RelevanceEngine",
    "StructuralSimilarityEngine",
]
