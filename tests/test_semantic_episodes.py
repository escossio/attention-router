from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from attention_router.application.cognitive_graph import build_cognitive_graph_slice
from attention_router.application.semantic_episodes import (
    SemanticEpisodeError,
    assign_timeline_event_to_episode,
    record_episode_lineage,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.cognitive_graph import (
    CognitiveNodeKind,
    CognitiveRelationKind,
)
from attention_router.domain.models import now_utc
from attention_router.infrastructure.models import (
    ExecutionIntentRow,
    OutboxMessageRow,
    RelationshipRow,
    ResourceRow,
    TenantRow,
    TimelineEventRow,
)
from attention_router.infrastructure.semantic_episode_models import (
    SemanticEpisodeMembershipRow,
    SemanticEpisodeRow,
)


def _resource(session, suffix: str = "1", *, tenant_id: str = DEFAULT_TENANT_ID):
    stamp = now_utc()
    row = ResourceRow(
        id=f"resource-{suffix}-{tenant_id[-4:]}",
        tenant_id=tenant_id,
        resource_type="PROPERTY",
        canonical_name=f"Casa {suffix}",
        status="ACTIVE",
        metadata_json={},
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    session.flush()
    return row


def _relationship(
    session,
    resource: ResourceRow,
    suffix: str = "1",
):
    stamp = now_utc()
    row = RelationshipRow(
        id=f"relationship-{suffix}-{resource.tenant_id[-4:]}",
        tenant_id=resource.tenant_id,
        source_entity_type="ACTOR",
        source_entity_id=f"actor-{suffix}",
        target_entity_type="RESOURCE",
        target_entity_id=resource.id,
        relationship_type="OCCUPIES",
        status="ACTIVE",
        valid_from=stamp - timedelta(days=100),
        valid_until=None,
        metadata_json={},
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    session.flush()
    return row


def _event(
    session,
    *,
    tenant_id: str,
    suffix: str,
    occurred_at,
    resource_id: str | None = None,
    relationship_id: str | None = None,
    visibility: str = "PRIVATE",
):
    row = TimelineEventRow(
        id=f"timeline-{suffix}-{tenant_id[-4:]}",
        tenant_id=tenant_id,
        canonical_event_id=None,
        actor_id=f"actor-{suffix}",
        relationship_id=relationship_id,
        resource_id=resource_id,
        event_type="RENT_ACTIVITY",
        event_ref={"source": "test"},
        occurred_at=occurred_at,
        visibility=visibility,
        provenance="TEST",
        metadata_json={},
    )
    session.add(row)
    session.flush()
    return row


def test_v2d_resource_scoped_events_group_and_replay_idempotently(session):
    stamp = now_utc()
    resource = _resource(session)
    first_event = _event(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        suffix="r1",
        occurred_at=stamp,
        resource_id=resource.id,
    )
    second_event = _event(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        suffix="r2",
        occurred_at=stamp + timedelta(days=3),
        resource_id=resource.id,
    )

    first = assign_timeline_event_to_episode(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        event_id=first_event.id,
        episode_type="RENT_MATTER",
        scope_type="RESOURCE",
        max_inactivity=timedelta(days=14),
    )
    second = assign_timeline_event_to_episode(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        event_id=second_event.id,
        episode_type="RENT_MATTER",
        scope_type="RESOURCE",
        max_inactivity=timedelta(days=14),
    )
    replay = assign_timeline_event_to_episode(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        event_id=first_event.id,
        episode_type="RENT_MATTER",
        scope_type="RESOURCE",
        max_inactivity=timedelta(days=14),
    )

    assert first.created_episode is True
    assert second.created_episode is False
    assert first.episode.id == second.episode.id
    assert replay.episode.id == first.episode.id
    assert replay.membership.id == first.membership.id
    assert replay.created_membership is False
    assert session.scalar(
        select(func.count()).select_from(SemanticEpisodeRow)
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(SemanticEpisodeMembershipRow)
    ) == 2

    graph = build_cognitive_graph_slice(session, DEFAULT_TENANT_ID)
    assert any(
        node.kind is CognitiveNodeKind.EPISODE
        and node.source_id == first.episode.id
        for node in graph.nodes
    )
    assert any(
        edge.relation_kind is CognitiveRelationKind.EPISODE_RESOURCE
        and edge.source_node_id == f"episode:{first.episode.id}"
        and edge.target_node_id == f"resource:{resource.id}"
        for edge in graph.edges
    )
    assert sum(
        edge.relation_kind is CognitiveRelationKind.EPISODE_MEMBER
        and edge.source_node_id == f"episode:{first.episode.id}"
        for edge in graph.edges
    ) == 2


def test_v2d_relationship_scoped_episode_uses_exact_relationship(session):
    stamp = now_utc()
    resource = _resource(session, "2")
    relationship = _relationship(session, resource, "2")
    event = _event(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        suffix="relationship",
        occurred_at=stamp,
        resource_id=resource.id,
        relationship_id=relationship.id,
    )

    result = assign_timeline_event_to_episode(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        event_id=event.id,
        episode_type="TENANCY_MATTER",
        scope_type="RELATIONSHIP",
    )

    assert result.episode.scope_type == "RELATIONSHIP"
    assert result.episode.scope_ref == relationship.id
    assert result.membership.association_reason == "EXACT_RELATIONSHIP"

    graph = build_cognitive_graph_slice(session, DEFAULT_TENANT_ID)
    assert any(
        edge.relation_kind is CognitiveRelationKind.EPISODE_RELATIONSHIP
        and edge.target_node_id == f"relationship:{relationship.id}"
        for edge in graph.edges
    )


def test_v2d_temporal_gap_creates_distinct_episodes(session):
    stamp = now_utc()
    resource = _resource(session, "3")
    first = _event(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        suffix="gap-a",
        occurred_at=stamp,
        resource_id=resource.id,
    )
    second = _event(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        suffix="gap-b",
        occurred_at=stamp + timedelta(days=60),
        resource_id=resource.id,
    )

    first_result = assign_timeline_event_to_episode(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        event_id=first.id,
        episode_type="PROPERTY_MATTER",
        scope_type="RESOURCE",
        max_inactivity=timedelta(days=14),
    )
    second_result = assign_timeline_event_to_episode(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        event_id=second.id,
        episode_type="PROPERTY_MATTER",
        scope_type="RESOURCE",
        max_inactivity=timedelta(days=14),
    )

    assert first_result.episode.id != second_result.episode.id
    assert session.scalar(
        select(func.count()).select_from(SemanticEpisodeRow)
    ) == 2


def test_v2d_cross_tenant_assignment_fails_closed(session):
    tenant_b = "00000000-0000-4000-8000-00000000d223"
    stamp = now_utc()
    session.add(
        TenantRow(
            id=tenant_b,
            slug="v2d-tenant-b",
            name="V2D Tenant B",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()
    resource = _resource(session, "b", tenant_id=tenant_b)
    event = _event(
        session,
        tenant_id=tenant_b,
        suffix="tenant-b",
        occurred_at=stamp,
        resource_id=resource.id,
    )

    with pytest.raises(
        SemanticEpisodeError,
        match="SEMANTIC_EPISODE_TENANT_MISMATCH",
    ):
        assign_timeline_event_to_episode(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            event_id=event.id,
            episode_type="PROPERTY_MATTER",
            scope_type="RESOURCE",
        )


def test_v2d_secret_episode_is_hidden_from_default_graph(session):
    stamp = now_utc()
    resource = _resource(session, "secret")
    event = _event(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        suffix="secret",
        occurred_at=stamp,
        resource_id=resource.id,
        visibility="SECRET",
    )
    result = assign_timeline_event_to_episode(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        event_id=event.id,
        episode_type="PRIVATE_MATTER",
        scope_type="RESOURCE",
    )
    assert result.episode.sensitivity_class == "SECRET"

    default_graph = build_cognitive_graph_slice(
        session,
        DEFAULT_TENANT_ID,
    )
    assert f"episode:{result.episode.id}" not in default_graph.node_index()

    privileged_graph = build_cognitive_graph_slice(
        session,
        DEFAULT_TENANT_ID,
        include_secret=True,
    )
    assert f"episode:{result.episode.id}" in privileged_graph.node_index()


def test_v2d_lineage_merge_preserves_source_memberships(session):
    stamp = now_utc()
    sources = []
    for index in range(3):
        resource = _resource(session, f"lineage-{index}")
        event = _event(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            suffix=f"lineage-{index}",
            occurred_at=stamp + timedelta(days=index),
            resource_id=resource.id,
        )
        result = assign_timeline_event_to_episode(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            event_id=event.id,
            episode_type="PROPERTY_MATTER",
            scope_type="RESOURCE",
        )
        sources.append(result)

    before_memberships = session.scalar(
        select(func.count()).select_from(SemanticEpisodeMembershipRow)
    )

    target = record_episode_lineage(
        session,
        target_episode_id=sources[2].episode.id,
        source_episode_ids=[
            sources[0].episode.id,
            sources[1].episode.id,
        ],
        lineage_kind="MERGE",
    )

    assert target.merged_from_episode_ids == sorted(
        [sources[0].episode.id, sources[1].episode.id]
    )
    assert session.get(
        SemanticEpisodeRow, sources[0].episode.id
    ).state == "MERGED"
    assert session.get(
        SemanticEpisodeRow, sources[1].episode.id
    ).state == "MERGED"
    assert session.scalar(
        select(func.count()).select_from(SemanticEpisodeMembershipRow)
    ) == before_memberships



def test_v2d_lineage_propagates_secret_sensitivity_to_target(session):
    stamp = now_utc()
    results = []
    for index, visibility in enumerate(("SECRET", "PRIVATE", "PRIVATE")):
        resource = _resource(session, f"secret-lineage-{index}")
        event = _event(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            suffix=f"secret-lineage-{index}",
            occurred_at=stamp + timedelta(days=index),
            resource_id=resource.id,
            visibility=visibility,
        )
        results.append(
            assign_timeline_event_to_episode(
                session,
                tenant_id=DEFAULT_TENANT_ID,
                event_id=event.id,
                episode_type="PROPERTY_MATTER",
                scope_type="RESOURCE",
            )
        )

    target = record_episode_lineage(
        session,
        target_episode_id=results[2].episode.id,
        source_episode_ids=[
            results[0].episode.id,
            results[1].episode.id,
        ],
        lineage_kind="MERGE",
    )

    assert target.sensitivity_class == "SECRET"

    default_graph = build_cognitive_graph_slice(
        session,
        DEFAULT_TENANT_ID,
    )
    assert f"episode:{target.id}" not in default_graph.node_index()

    privileged_graph = build_cognitive_graph_slice(
        session,
        DEFAULT_TENANT_ID,
        include_secret=True,
    )
    assert f"episode:{target.id}" in privileged_graph.node_index()

def test_v2d_episode_builder_creates_zero_execution_or_outbound_authority(session):
    resource = _resource(session, "safe")
    event = _event(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        suffix="safe",
        occurred_at=now_utc(),
        resource_id=resource.id,
    )

    assign_timeline_event_to_episode(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        event_id=event.id,
        episode_type="PROPERTY_MATTER",
        scope_type="RESOURCE",
    )

    assert session.scalar(
        select(func.count()).select_from(ExecutionIntentRow)
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(OutboxMessageRow)
    ) == 0
