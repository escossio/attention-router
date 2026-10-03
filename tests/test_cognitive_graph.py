from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from attention_router.application.cognitive_graph import build_cognitive_graph_slice
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.cognitive_graph import (
    CognitiveInferenceClass,
    CognitiveNodeKind,
    CognitiveRelationKind,
)
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    EntityStateRow,
    ExecutionIntentRow,
    FactRow,
    MemoryActorRow,
    MemoryClaimRow,
    OutboxMessageRow,
    RelationshipRow,
    ResourceRow,
    TenantRow,
    TimelineEventRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding


def _memory_actor(session, tenant_id: str, actor_key: str) -> MemoryActorRow:
    stamp = now_utc()
    row = MemoryActorRow(
        id=new_id(),
        tenant_id=tenant_id,
        actor_key=actor_key,
        metadata_json={},
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    session.flush()
    return row


def _claim(
    session,
    actor: MemoryActorRow,
    predicate: str,
    *,
    sensitivity: str = "NORMAL",
) -> MemoryClaimRow:
    stamp = now_utc()
    row = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=actor.id,
        subject_entity_id=None,
        predicate=predicate,
        object_type="TEXT",
        object_text="synthetic-value",
        object_actor_id=None,
        object_entity_id=None,
        object_json=None,
        context={},
        confidence=0.91,
        sensitivity_class=sensitivity,
        source_quality="SELF_REPORTED",
        valid_from=stamp - timedelta(minutes=1),
        valid_until=None,
        status="ACTIVE",
        staleness_class="STABLE",
        supersedes_claim_id=None,
        conflict_group_id=None,
        first_observed_at=stamp,
        last_observed_at=stamp,
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    session.flush()
    return row


def _seed_graph(session, tenant_id: str = DEFAULT_TENANT_ID) -> dict[str, str]:
    stamp = now_utc()
    upsert_actor_binding(
        session,
        "test",
        f"{tenant_id}:owner-ext",
        f"{tenant_id}:owner",
        "owner",
        display_name="Nilvanda",
        metadata={"owner": True},
        tenant_id=tenant_id,
    )
    upsert_actor_binding(
        session,
        "test",
        f"{tenant_id}:tenant-ext",
        f"{tenant_id}:angelo",
        "family_or_business",
        display_name="Ângelo",
        metadata={},
        tenant_id=tenant_id,
    )
    owner_memory = _memory_actor(session, tenant_id, f"{tenant_id}:owner")
    _claim(session, owner_memory, "identity.preferred_name")
    _claim(
        session,
        owner_memory,
        "security.must_not_project",
        sensitivity="SECRET",
    )

    resource = ResourceRow(
        id=f"{tenant_id}:property-1",
        tenant_id=tenant_id,
        resource_type="PROPERTY",
        canonical_name="Casa 01",
        status="ACTIVE",
        metadata_json={},
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(resource)

    owns = RelationshipRow(
        id=f"{tenant_id}:rel-owns",
        tenant_id=tenant_id,
        source_entity_type="ACTOR",
        source_entity_id=f"{tenant_id}:owner",
        target_entity_type="RESOURCE",
        target_entity_id=resource.id,
        relationship_type="OWNS",
        status="ACTIVE",
        valid_from=stamp - timedelta(days=100),
        valid_until=None,
        metadata_json={},
        created_at=stamp,
        updated_at=stamp,
    )
    occupies = RelationshipRow(
        id=f"{tenant_id}:rel-occupies",
        tenant_id=tenant_id,
        source_entity_type="ACTOR",
        source_entity_id=f"{tenant_id}:angelo",
        target_entity_type="RESOURCE",
        target_entity_id=resource.id,
        relationship_type="OCCUPIES",
        status="ACTIVE",
        valid_from=stamp - timedelta(days=60),
        valid_until=None,
        metadata_json={},
        created_at=stamp,
        updated_at=stamp,
    )
    session.add_all([owns, occupies])
    session.flush()

    session.add(
        TimelineEventRow(
            id=f"{tenant_id}:event-rent-request",
            tenant_id=tenant_id,
            canonical_event_id=None,
            actor_id=f"{tenant_id}:angelo",
            relationship_id=occupies.id,
            resource_id=resource.id,
            event_type="RENT_PAYMENT_REQUEST",
            event_ref={"pattern_key": "rent:property-1"},
            occurred_at=stamp,
            visibility="PRIVATE",
            provenance="TEST",
            metadata_json={},
        )
    )
    session.add(
        FactRow(
            id=f"{tenant_id}:fact-property-count",
            tenant_id=tenant_id,
            subject_type="ACTOR",
            subject_id=f"{tenant_id}:owner",
            predicate="property.portfolio_member",
            value_json={"resource_id": resource.id},
            value_ref=resource.id,
            fact_class="ASSERTED",
            source_type="USER_INPUT",
            source_ref="synthetic-fixture",
            confidence=1.0,
            observed_at=stamp,
            valid_from=stamp,
            valid_until=None,
            supersedes_fact_id=None,
            metadata_json={},
            created_at=stamp,
        )
    )
    session.add(
        EntityStateRow(
            id=f"{tenant_id}:state-occupancy",
            tenant_id=tenant_id,
            subject_type="RESOURCE",
            subject_id=resource.id,
            state_namespace="occupancy",
            state_key="status",
            state_value={"value": "OCCUPIED"},
            source="TEST",
            effective_at=stamp,
            expires_at=None,
            version=1,
            updated_at=stamp,
        )
    )
    session.flush()
    return {
        "owner": f"{tenant_id}:owner",
        "angelo": f"{tenant_id}:angelo",
        "resource": resource.id,
        "occupies": occupies.id,
    }


def test_v2a_projects_existing_world_primitives_without_inference(session):
    ids = _seed_graph(session)

    graph = build_cognitive_graph_slice(session, DEFAULT_TENANT_ID)
    graph.validate()
    nodes = graph.node_index()

    assert nodes[f"person:{ids['owner']}"].kind is CognitiveNodeKind.PERSON
    assert nodes[f"person:{ids['owner']}"].label == "Nilvanda"
    assert nodes[f"person:{ids['angelo']}"].label == "Ângelo"
    assert nodes[f"resource:{ids['resource']}"].label == "Casa 01"
    assert f"relationship:{ids['occupies']}" in nodes
    assert f"event:{DEFAULT_TENANT_ID}:event-rent-request" in nodes
    assert any(node.kind is CognitiveNodeKind.CLAIM for node in graph.nodes)
    assert any(node.kind is CognitiveNodeKind.FACT for node in graph.nodes)
    assert any(node.kind is CognitiveNodeKind.STATE for node in graph.nodes)

    semantic = [
        edge
        for edge in graph.edges
        if edge.relation_kind is CognitiveRelationKind.EXPLICIT_RELATION
        and edge.semantic_relation == "OCCUPIES"
    ]
    assert len(semantic) == 1
    assert semantic[0].source_node_id == f"person:{ids['angelo']}"
    assert semantic[0].target_node_id == f"resource:{ids['resource']}"
    assert semantic[0].inference_class is CognitiveInferenceClass.EXPLICIT

    assert any(
        edge.relation_kind is CognitiveRelationKind.EVENT_RESOURCE
        and edge.target_node_id == f"resource:{ids['resource']}"
        for edge in graph.edges
    )
    assert any(
        edge.relation_kind is CognitiveRelationKind.EVENT_RELATIONSHIP
        and edge.target_node_id == f"relationship:{ids['occupies']}"
        for edge in graph.edges
    )
    assert not any(
        node.label == "security.must_not_project"
        for node in graph.nodes
    )


def test_v2a_graph_is_tenant_scoped(session):
    tenant_b = "00000000-0000-4000-8000-000000000099"
    stamp = now_utc()
    session.add(
        TenantRow(
            id=tenant_b,
            slug="cognitive-v2a-b",
            name="Cognitive V2A B",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()

    _seed_graph(session, DEFAULT_TENANT_ID)
    _seed_graph(session, tenant_b)

    graph_a = build_cognitive_graph_slice(session, DEFAULT_TENANT_ID)
    graph_b = build_cognitive_graph_slice(session, tenant_b)

    assert graph_a.tenant_id == DEFAULT_TENANT_ID
    assert graph_b.tenant_id == tenant_b
    assert all(node.tenant_id == DEFAULT_TENANT_ID for node in graph_a.nodes)
    assert all(node.tenant_id == tenant_b for node in graph_b.nodes)
    assert not {
        node.node_id for node in graph_a.nodes
    } & {
        node.node_id for node in graph_b.nodes
    }


def test_v2a_projection_is_read_only_and_non_actionable(session):
    _seed_graph(session)

    before = {
        "resources": session.scalar(select(func.count()).select_from(ResourceRow)),
        "relationships": session.scalar(
            select(func.count()).select_from(RelationshipRow)
        ),
        "timeline": session.scalar(
            select(func.count()).select_from(TimelineEventRow)
        ),
        "claims": session.scalar(
            select(func.count()).select_from(MemoryClaimRow)
        ),
        "facts": session.scalar(select(func.count()).select_from(FactRow)),
        "states": session.scalar(select(func.count()).select_from(EntityStateRow)),
        "execution_intents": session.scalar(
            select(func.count()).select_from(ExecutionIntentRow)
        ),
        "outbox": session.scalar(
            select(func.count()).select_from(OutboxMessageRow)
        ),
    }

    graph = build_cognitive_graph_slice(session, DEFAULT_TENANT_ID)
    assert graph.nodes
    assert graph.edges

    after = {
        "resources": session.scalar(select(func.count()).select_from(ResourceRow)),
        "relationships": session.scalar(
            select(func.count()).select_from(RelationshipRow)
        ),
        "timeline": session.scalar(
            select(func.count()).select_from(TimelineEventRow)
        ),
        "claims": session.scalar(
            select(func.count()).select_from(MemoryClaimRow)
        ),
        "facts": session.scalar(select(func.count()).select_from(FactRow)),
        "states": session.scalar(select(func.count()).select_from(EntityStateRow)),
        "execution_intents": session.scalar(
            select(func.count()).select_from(ExecutionIntentRow)
        ),
        "outbox": session.scalar(
            select(func.count()).select_from(OutboxMessageRow)
        ),
    }

    assert after == before
    assert after["execution_intents"] == 0
    assert after["outbox"] == 0
