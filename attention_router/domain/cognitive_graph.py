"""Typed, read-only contracts for the Personal Context V2 Cognitive Graph.

These contracts do not grant authority, mutate source-of-truth rows, or imply
that a projected relation is an inferred fact.  V2A is a symbolic projection
layer over already-governed repository primitives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class CognitiveNodeKind(StrEnum):
    PERSON = "PERSON"
    ENTITY = "ENTITY"
    RESOURCE = "RESOURCE"
    RELATIONSHIP = "RELATIONSHIP"
    EVENT = "EVENT"
    CLAIM = "CLAIM"
    FACT = "FACT"
    STATE = "STATE"


class CognitiveRelationKind(StrEnum):
    EXPLICIT_RELATION = "EXPLICIT_RELATION"
    RELATIONSHIP_SOURCE = "RELATIONSHIP_SOURCE"
    RELATIONSHIP_TARGET = "RELATIONSHIP_TARGET"
    EVENT_ACTOR = "EVENT_ACTOR"
    EVENT_RESOURCE = "EVENT_RESOURCE"
    EVENT_RELATIONSHIP = "EVENT_RELATIONSHIP"
    CLAIM_SUBJECT = "CLAIM_SUBJECT"
    CLAIM_OBJECT = "CLAIM_OBJECT"
    FACT_SUBJECT = "FACT_SUBJECT"
    STATE_SUBJECT = "STATE_SUBJECT"
    IDENTITY_ALIAS = "IDENTITY_ALIAS"


class CognitiveInferenceClass(StrEnum):
    EXPLICIT = "EXPLICIT"
    STRUCTURAL_PROJECTION = "STRUCTURAL_PROJECTION"


@dataclass(frozen=True, slots=True)
class CognitiveNode:
    node_id: str
    tenant_id: str
    kind: CognitiveNodeKind
    source_type: str
    source_id: str
    label: str | None = None
    confidence: float | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CognitiveEdge:
    edge_id: str
    tenant_id: str
    source_node_id: str
    target_node_id: str
    relation_kind: CognitiveRelationKind
    semantic_relation: str | None = None
    inference_class: CognitiveInferenceClass = CognitiveInferenceClass.STRUCTURAL_PROJECTION
    confidence: float | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CognitiveGraphSlice:
    tenant_id: str
    nodes: tuple[CognitiveNode, ...]
    edges: tuple[CognitiveEdge, ...]
    generated_at: datetime

    def node_index(self) -> dict[str, CognitiveNode]:
        return {node.node_id: node for node in self.nodes}

    def validate(self) -> None:
        ids = set()
        for node in self.nodes:
            if node.tenant_id != self.tenant_id:
                raise ValueError("COGNITIVE_GRAPH_NODE_TENANT_MISMATCH")
            if node.node_id in ids:
                raise ValueError("COGNITIVE_GRAPH_DUPLICATE_NODE")
            ids.add(node.node_id)

        edge_ids = set()
        for edge in self.edges:
            if edge.tenant_id != self.tenant_id:
                raise ValueError("COGNITIVE_GRAPH_EDGE_TENANT_MISMATCH")
            if edge.edge_id in edge_ids:
                raise ValueError("COGNITIVE_GRAPH_DUPLICATE_EDGE")
            edge_ids.add(edge.edge_id)
            if edge.source_node_id not in ids or edge.target_node_id not in ids:
                raise ValueError("COGNITIVE_GRAPH_DANGLING_EDGE")


__all__ = [
    "CognitiveEdge",
    "CognitiveGraphSlice",
    "CognitiveInferenceClass",
    "CognitiveNode",
    "CognitiveNodeKind",
    "CognitiveRelationKind",
]
