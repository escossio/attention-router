from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from attention_router.application.cognitive_graph import (
    build_cognitive_graph_slice,
)
from attention_router.application.graph_intelligence import (
    DeterministicAnomalyFeatureEngine,
    DeterministicContextPathRanker,
    DeterministicRelationCandidateEngine,
    DeterministicSpreadingActivationEngine,
    DeterministicStructuralSimilarityEngine,
    GraphIntelligenceError,
    default_graph_intelligence_engines,
    persist_relation_candidate_insight,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.graph_intelligence import (
    GraphIntelligenceBudget,
)
from attention_router.domain.models import now_utc
from attention_router.infrastructure.candidate_insight_models import (
    CandidateInsightEvidenceRow,
)
from attention_router.infrastructure.models import (
    ExecutionIntentRow,
    OutboxMessageRow,
    RelationshipRow,
    ResourceRow,
    TenantRow,
    TimelineEventRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding


def _tenant(session, tenant_id: str) -> None:
    if session.get(TenantRow, tenant_id) is not None:
        return
    stamp = now_utc()
    session.add(
        TenantRow(
            id=tenant_id,
            slug=f"v2i-{tenant_id[-8:]}",
            name=f"V2I {tenant_id[-8:]}",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()


def _event_world(
    session,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    suffix: str = "a",
    event_count: int = 2,
):
    _tenant(session, tenant_id)
    stamp = now_utc()
    actor_key = f"{tenant_id}:graph-person-{suffix}"
    upsert_actor_binding(
        session,
        source="test",
        external_actor_id=f"{tenant_id}:external-{suffix}",
        actor_key=actor_key,
        actor_category="contact",
        display_name=f"Pessoa {suffix}",
        metadata={},
        tenant_id=tenant_id,
    )
    resource = ResourceRow(
        id=f"{tenant_id}:graph-resource-{suffix}",
        tenant_id=tenant_id,
        resource_type="PROPERTY",
        canonical_name=f"Recurso {suffix}",
        status="ACTIVE",
        metadata_json={},
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(resource)
    session.flush()

    events = []
    for index in range(event_count):
        event = TimelineEventRow(
            id=f"{tenant_id}:graph-event-{suffix}-{index}",
            tenant_id=tenant_id,
            canonical_event_id=None,
            actor_id=actor_key,
            relationship_id=None,
            resource_id=resource.id,
            event_type="PROPERTY_ACTIVITY",
            event_ref={"source": "v2i-test", "index": index},
            occurred_at=stamp + timedelta(minutes=index),
            visibility="PRIVATE",
            provenance="TEST",
            metadata_json={},
        )
        session.add(event)
        events.append(event)
    session.flush()
    return actor_key, resource, tuple(events)


def test_v2i_spreading_activation_is_bounded_and_explainable(session):
    actor_key, resource, _events = _event_world(
        session,
        suffix="activation",
    )
    graph = build_cognitive_graph_slice(
        session,
        DEFAULT_TENANT_ID,
    )
    engine = DeterministicSpreadingActivationEngine()

    results = engine.activate(
        graph,
        seeds={f"person:{actor_key}": 1.0},
        budget=GraphIntelligenceBudget(
            max_hops=2,
            max_fanout=10,
            max_nodes=20,
            max_paths=8,
            max_candidates=8,
        ),
    )

    by_node = {item.node_id: item for item in results}
    resource_result = by_node[f"resource:{resource.id}"]
    assert 0 < resource_result.activation <= 1
    assert resource_result.hop_count == 2
    assert len(resource_result.path) == 2
    assert resource_result.path[0].relation_kind == "EVENT_ACTOR"
    assert resource_result.path[0].direction == "INBOUND"
    assert resource_result.path[1].relation_kind == "EVENT_RESOURCE"
    assert resource_result.path[1].direction == "OUTBOUND"

    one_hop = engine.activate(
        graph,
        seeds={f"person:{actor_key}": 1.0},
        budget=GraphIntelligenceBudget(
            max_hops=1,
            max_fanout=10,
            max_nodes=20,
            max_paths=8,
            max_candidates=8,
        ),
    )
    assert not any(
        item.node_id == f"resource:{resource.id}"
        for item in one_hop
    )


def test_v2i_structural_similarity_uses_typed_neighbor_signatures(session):
    _actor_a, resource_a, _ = _event_world(
        session,
        suffix="similar-a",
    )
    _actor_b, resource_b, _ = _event_world(
        session,
        suffix="similar-b",
    )
    graph = build_cognitive_graph_slice(
        session,
        DEFAULT_TENANT_ID,
    )

    results = DeterministicStructuralSimilarityEngine().similar_nodes(
        graph,
        node_id=f"resource:{resource_a.id}",
        budget=GraphIntelligenceBudget(max_candidates=10),
    )

    match = next(
        item
        for item in results
        if item.compared_node_id == f"resource:{resource_b.id}"
    )
    assert match.similarity == pytest.approx(1.0)
    assert match.shared_signature_count >= 1
    assert any(
        "EVENT_RESOURCE" in signature
        for signature in match.shared_signatures
    )


def test_v2i_path_ranker_returns_deterministic_explanation_paths(session):
    actor_key, resource, events = _event_world(
        session,
        suffix="paths",
    )
    graph = build_cognitive_graph_slice(
        session,
        DEFAULT_TENANT_ID,
    )

    paths = DeterministicContextPathRanker().rank_paths(
        graph,
        source_node_id=f"person:{actor_key}",
        target_node_id=f"resource:{resource.id}",
        budget=GraphIntelligenceBudget(
            max_hops=2,
            max_fanout=10,
            max_nodes=20,
            max_paths=8,
            max_candidates=8,
        ),
    )

    assert len(paths) == len(events) == 2
    assert all(path.hops == 2 for path in paths)
    assert all(0 < path.score <= 1 for path in paths)
    assert all(
        [step.relation_kind for step in path.steps]
        == ["EVENT_ACTOR", "EVENT_RESOURCE"]
        for path in paths
    )
    assert paths == tuple(
        sorted(
            paths,
            key=lambda item: (
                -item.score,
                item.hops,
                tuple(step.edge_id for step in item.steps),
            ),
        )
    )


def test_v2i_relation_candidate_requires_repeated_independent_events(session):
    actor_key, resource, events = _event_world(
        session,
        suffix="candidate",
    )
    graph = build_cognitive_graph_slice(
        session,
        DEFAULT_TENANT_ID,
    )

    candidates = DeterministicRelationCandidateEngine().candidates(
        graph,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.subject_node_id == f"person:{actor_key}"
    assert candidate.target_node_id == f"resource:{resource.id}"
    assert candidate.relation_hint == "REPEATED_EVENT_COOCCURRENCE"
    assert candidate.support_event_ids == tuple(
        sorted(event.id for event in events)
    )
    assert candidate.confidence == pytest.approx(0.75)
    assert candidate.provenance["grants_authority"] is False
    assert candidate.provenance["materializes_canonical_truth"] is False

    _single_actor, _single_resource, _ = _event_world(
        session,
        suffix="candidate-single",
        event_count=1,
    )
    graph_after_single = build_cognitive_graph_slice(
        session,
        DEFAULT_TENANT_ID,
    )
    assert not any(
        item.subject_node_id == f"person:{_single_actor}"
        and item.target_node_id == f"resource:{_single_resource.id}"
        for item in DeterministicRelationCandidateEngine().candidates(
            graph_after_single
        )
    )


def test_v2i_explicit_relationship_suppresses_structural_link_candidate(session):
    actor_key, resource, _events = _event_world(
        session,
        suffix="explicit",
    )
    stamp = now_utc()
    session.add(
        RelationshipRow(
            id=f"{DEFAULT_TENANT_ID}:v2i-explicit",
            tenant_id=DEFAULT_TENANT_ID,
            source_entity_type="ACTOR",
            source_entity_id=actor_key,
            target_entity_type="RESOURCE",
            target_entity_id=resource.id,
            relationship_type="USES",
            status="ACTIVE",
            valid_from=stamp - timedelta(days=1),
            valid_until=None,
            metadata_json={},
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()
    graph = build_cognitive_graph_slice(
        session,
        DEFAULT_TENANT_ID,
    )

    assert not any(
        item.subject_node_id == f"person:{actor_key}"
        and item.target_node_id == f"resource:{resource.id}"
        for item in DeterministicRelationCandidateEngine().candidates(graph)
    )


def test_v2i_relation_candidate_crosses_only_candidate_insight_boundary(session):
    actor_key, resource, events = _event_world(
        session,
        suffix="persist",
    )
    graph = build_cognitive_graph_slice(
        session,
        DEFAULT_TENANT_ID,
    )
    candidate = next(
        item
        for item in DeterministicRelationCandidateEngine().candidates(graph)
        if item.subject_node_id == f"person:{actor_key}"
        and item.target_node_id == f"resource:{resource.id}"
    )
    before = {
        "relationships": session.scalar(
            select(func.count()).select_from(RelationshipRow)
        ),
        "execution": session.scalar(
            select(func.count()).select_from(ExecutionIntentRow)
        ),
        "outbox": session.scalar(
            select(func.count()).select_from(OutboxMessageRow)
        ),
    }

    insight, created = persist_relation_candidate_insight(
        session,
        graph=graph,
        candidate=candidate,
    )
    replay, replay_created = persist_relation_candidate_insight(
        session,
        graph=graph,
        candidate=candidate,
    )

    assert created is True
    assert replay_created is False
    assert replay.id == insight.id
    assert insight.state == "PROPOSED"
    assert insight.insight_type == "RELATIONSHIP_PROPOSAL"
    assert insight.source_engine == "RULE"
    assert insight.subject_type == "PERSON"
    assert insight.subject_id == actor_key
    assert insight.predicate == "context.graph.structural_association"
    assert insight.proposed_value == {
        "target_type": "RESOURCE",
        "target_id": resource.id,
        "relation_hint": "REPEATED_EVENT_COOCCURRENCE",
        "support_event_count": 2,
    }
    assert insight.provenance["governance"] == "CANDIDATE_ONLY"
    assert insight.provenance["grants_authority"] is False
    assert insight.provenance["materializes_canonical_truth"] is False

    evidence = list(
        session.scalars(
            select(CandidateInsightEvidenceRow)
            .where(
                CandidateInsightEvidenceRow.candidate_id == insight.id
            )
            .order_by(CandidateInsightEvidenceRow.source_ref)
        ).all()
    )
    assert [row.evidence_type for row in evidence] == [
        "TIMELINE_EVENT",
        "TIMELINE_EVENT",
    ]
    assert {row.source_ref for row in evidence} == {
        event.id for event in events
    }

    after = {
        "relationships": session.scalar(
            select(func.count()).select_from(RelationshipRow)
        ),
        "execution": session.scalar(
            select(func.count()).select_from(ExecutionIntentRow)
        ),
        "outbox": session.scalar(
            select(func.count()).select_from(OutboxMessageRow)
        ),
    }
    assert after == before


def test_v2i_persistence_rejects_forged_relation_candidate(session):
    actor_key, resource, _events = _event_world(
        session,
        suffix="forged",
    )
    graph = build_cognitive_graph_slice(
        session,
        DEFAULT_TENANT_ID,
    )
    candidate = next(
        item
        for item in DeterministicRelationCandidateEngine().candidates(graph)
        if item.subject_node_id == f"person:{actor_key}"
        and item.target_node_id == f"resource:{resource.id}"
    )
    forged = replace(candidate, confidence=0.99)

    with pytest.raises(
        GraphIntelligenceError,
        match="GRAPH_INTELLIGENCE_RELATION_CANDIDATE_MISMATCH",
    ):
        persist_relation_candidate_insight(
            session,
            graph=graph,
            candidate=forged,
        )


def test_v2i_anomaly_features_are_bounded_and_payload_free(session):
    actor_key, _resource, _events = _event_world(
        session,
        suffix="anomaly",
    )
    graph = build_cognitive_graph_slice(
        session,
        DEFAULT_TENANT_ID,
    )

    features = DeterministicAnomalyFeatureEngine().features(
        graph,
        node_id=f"person:{actor_key}",
    )

    assert features.degree == 2
    assert features.relation_kind_count == 1
    assert features.neighbor_kind_count == 1
    assert 0 <= features.low_confidence_edge_ratio <= 1
    assert 0 <= features.expired_edge_ratio <= 1
    assert features.isolated is False
    assert features.feature_version == "GRAPH_ANOMALY_FEATURES_V0"


def test_v2i_secret_evidence_never_supports_relation_candidate(session):
    actor_key, resource, events = _event_world(
        session,
        suffix="secret",
        event_count=2,
    )
    secret = TimelineEventRow(
        id=f"{DEFAULT_TENANT_ID}:graph-event-secret",
        tenant_id=DEFAULT_TENANT_ID,
        canonical_event_id=None,
        actor_id=actor_key,
        relationship_id=None,
        resource_id=resource.id,
        event_type="SECRET_ACTIVITY",
        event_ref={"source": "v2i-secret"},
        occurred_at=now_utc(),
        visibility="SECRET",
        provenance="TEST",
        metadata_json={},
    )
    session.add(secret)
    session.flush()

    graph = build_cognitive_graph_slice(
        session,
        DEFAULT_TENANT_ID,
    )
    candidate = next(
        item
        for item in DeterministicRelationCandidateEngine().candidates(graph)
        if item.subject_node_id == f"person:{actor_key}"
        and item.target_node_id == f"resource:{resource.id}"
    )

    assert secret.id not in candidate.support_event_ids
    assert candidate.support_event_ids == tuple(
        sorted(event.id for event in events)
    )


def test_v2i_engine_registry_is_replaceable_and_budget_fail_closed(session):
    actor_key, _resource, _events = _event_world(
        session,
        suffix="registry",
    )
    graph = build_cognitive_graph_slice(
        session,
        DEFAULT_TENANT_ID,
    )
    engines = default_graph_intelligence_engines()

    activation = engines.relevance.activate(
        graph,
        seeds={f"person:{actor_key}": 1.0},
    )
    assert activation
    assert engines.relevance.engine_version == "V0"
    assert engines.similarity.engine_version == "V0"
    assert engines.relation_candidates.engine_version == "V0"
    assert engines.anomaly.engine_version == "V0"
    assert engines.path_ranker.engine_version == "V0"

    with pytest.raises(
        GraphIntelligenceError,
        match="GRAPH_INTELLIGENCE_HOPS_OUT_OF_RANGE",
    ):
        engines.relevance.activate(
            graph,
            seeds={f"person:{actor_key}": 1.0},
            budget=GraphIntelligenceBudget(max_hops=5),
        )


def test_v2i_graph_slice_keeps_tenant_isolation(session):
    tenant_b = "00000000-0000-4000-8000-00000000a228"
    actor_a, resource_a, _ = _event_world(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        suffix="tenant-a",
    )
    actor_b, resource_b, _ = _event_world(
        session,
        tenant_id=tenant_b,
        suffix="tenant-b",
    )

    graph_a = build_cognitive_graph_slice(
        session,
        DEFAULT_TENANT_ID,
    )
    graph_b = build_cognitive_graph_slice(
        session,
        tenant_b,
    )
    candidates_a = DeterministicRelationCandidateEngine().candidates(
        graph_a
    )
    candidates_b = DeterministicRelationCandidateEngine().candidates(
        graph_b
    )

    assert any(
        item.subject_node_id == f"person:{actor_a}"
        and item.target_node_id == f"resource:{resource_a.id}"
        for item in candidates_a
    )
    assert any(
        item.subject_node_id == f"person:{actor_b}"
        and item.target_node_id == f"resource:{resource_b.id}"
        for item in candidates_b
    )
    assert not any(
        tenant_b in item.subject_node_id or tenant_b in item.target_node_id
        for item in candidates_a
    )
