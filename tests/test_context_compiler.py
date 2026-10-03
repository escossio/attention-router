from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from attention_router.application.cognitive_graph import build_cognitive_graph_slice
from attention_router.application.context_compiler import (
    ContextCompilerError,
    compile_graph_context,
)
from attention_router.application.semantic_episodes import (
    assign_timeline_event_to_episode,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.cognitive_graph import CognitiveRelationKind
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


def _ensure_tenant(session, tenant_id: str) -> None:
    if session.get(TenantRow, tenant_id) is not None:
        return
    stamp = now_utc()
    session.add(
        TenantRow(
            id=tenant_id,
            slug=f"v2f-{tenant_id[-8:]}",
            name=f"V2F {tenant_id[-8:]}",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()


def _memory_actor(
    session,
    *,
    tenant_id: str,
    actor_key: str,
) -> MemoryActorRow:
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
    *,
    predicate: str,
    value: str,
    sensitivity: str = "NORMAL",
) -> MemoryClaimRow:
    stamp = now_utc()
    row = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=actor.id,
        subject_entity_id=None,
        predicate=predicate,
        object_type="TEXT",
        object_text=value,
        object_actor_id=None,
        object_entity_id=None,
        object_json=None,
        context={},
        confidence=0.95,
        sensitivity_class=sensitivity,
        source_quality="SELF_REPORTED",
        valid_from=stamp - timedelta(days=1),
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


def _seed_world(
    session,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    suffix: str = "a",
):
    _ensure_tenant(session, tenant_id)
    stamp = now_utc()
    owner_key = f"{tenant_id}:owner"
    owner_external = f"{tenant_id}:owner-ext"
    angelo_key = f"{tenant_id}:angelo"
    upsert_actor_binding(
        session,
        "test",
        owner_external,
        owner_key,
        "owner",
        display_name="Leonardo",
        metadata={"owner": True},
        tenant_id=tenant_id,
    )
    upsert_actor_binding(
        session,
        "test",
        f"{tenant_id}:angelo-ext",
        angelo_key,
        "contact",
        display_name="Ângelo",
        metadata={},
        tenant_id=tenant_id,
    )
    owner_actor = _memory_actor(
        session,
        tenant_id=tenant_id,
        actor_key=owner_external,
    )

    resource = ResourceRow(
        id=f"{tenant_id}:property-07-{suffix}",
        tenant_id=tenant_id,
        resource_type="PROPERTY",
        canonical_name=f"Casa 07 {suffix}",
        status="ACTIVE",
        metadata_json={},
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(resource)

    relationship = RelationshipRow(
        id=f"{tenant_id}:occupies-{suffix}",
        tenant_id=tenant_id,
        source_entity_type="ACTOR",
        source_entity_id=angelo_key,
        target_entity_type="RESOURCE",
        target_entity_id=resource.id,
        relationship_type="OCCUPIES",
        status="ACTIVE",
        valid_from=stamp - timedelta(days=400),
        valid_until=None,
        metadata_json={},
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(relationship)
    session.flush()

    recent = TimelineEventRow(
        id=f"{tenant_id}:event-recent-{suffix}",
        tenant_id=tenant_id,
        canonical_event_id=None,
        actor_id=angelo_key,
        relationship_id=relationship.id,
        resource_id=resource.id,
        event_type="RENT_ACTIVITY",
        event_ref={"source": "recent"},
        occurred_at=stamp - timedelta(days=1),
        visibility="PRIVATE",
        provenance="TEST",
        metadata_json={},
    )
    old = TimelineEventRow(
        id=f"{tenant_id}:event-old-{suffix}",
        tenant_id=tenant_id,
        canonical_event_id=None,
        actor_id=angelo_key,
        relationship_id=relationship.id,
        resource_id=resource.id,
        event_type="RENT_ACTIVITY",
        event_ref={"source": "old"},
        occurred_at=stamp - timedelta(days=180),
        visibility="PRIVATE",
        provenance="TEST",
        metadata_json={},
    )
    secret = TimelineEventRow(
        id=f"{tenant_id}:event-secret-{suffix}",
        tenant_id=tenant_id,
        canonical_event_id=None,
        actor_id=angelo_key,
        relationship_id=relationship.id,
        resource_id=resource.id,
        event_type="CONFIDENTIAL_ACTIVITY",
        event_ref={"source": "secret"},
        occurred_at=stamp,
        visibility="SECRET",
        provenance="TEST",
        metadata_json={},
    )
    session.add_all([recent, old, secret])

    fact = FactRow(
        id=f"{tenant_id}:fact-rent-{suffix}",
        tenant_id=tenant_id,
        subject_type="RESOURCE",
        subject_id=resource.id,
        predicate="rent.expected_amount",
        value_json={"amount": 1350, "currency": "BRL"},
        value_ref=None,
        fact_class="ASSERTED",
        source_type="USER_INPUT",
        source_ref="v2f-fixture",
        confidence=1.0,
        observed_at=stamp,
        valid_from=stamp - timedelta(days=30),
        valid_until=None,
        supersedes_fact_id=None,
        metadata_json={},
        created_at=stamp,
    )
    state = EntityStateRow(
        id=f"{tenant_id}:state-occupancy-{suffix}",
        tenant_id=tenant_id,
        subject_type="RESOURCE",
        subject_id=resource.id,
        state_namespace="occupancy",
        state_key="status",
        state_value={"value": "OCCUPIED"},
        source="TEST",
        effective_at=stamp - timedelta(days=60),
        expires_at=None,
        version=1,
        updated_at=stamp,
    )
    session.add_all([fact, state])
    session.flush()

    episode_result = assign_timeline_event_to_episode(
        session,
        tenant_id=tenant_id,
        event_id=recent.id,
        episode_type="RENT_MATTER",
        scope_type="RESOURCE",
        max_inactivity=timedelta(days=30),
    )

    return {
        "owner_key": owner_key,
        "owner_actor": owner_actor,
        "angelo_key": angelo_key,
        "resource": resource,
        "relationship": relationship,
        "recent": recent,
        "old": old,
        "secret": secret,
        "fact": fact,
        "state": state,
        "episode": episode_result.episode,
    }


def _item_by_source(packet, source_id: str):
    return next(item for item in packet.items if item.source_id == source_id)


def test_v2f_compiles_graph_context_with_explainable_path(session):
    world = _seed_world(session)

    packet = compile_graph_context(
        session,
        DEFAULT_TENANT_ID,
        "O Ângelo ainda não mandou nada?",
        max_hops=2,
        item_budget=20,
        token_budget=6000,
    )

    assert packet.retrieval_method == "GRAPH_AWARE_V1"
    assert packet.lexical_fallback_used is False
    assert f"person:{world['angelo_key']}" in packet.seed_node_ids

    resource = _item_by_source(packet, world["resource"].id)
    assert resource.hop_count == 1
    assert resource.path
    assert resource.path[0].relation_kind == "EXPLICIT_RELATION"
    assert resource.path[0].semantic_relation == "OCCUPIES"
    assert resource.provenance["grants_authority"] is False

    fact = _item_by_source(packet, world["fact"].id)
    assert fact.hop_count <= 2
    assert any(
        step.relation_kind == "FACT_SUBJECT"
        for step in fact.path
    )
    assert set(fact.score_components) == {
        "lexical_seed",
        "hop",
        "confidence",
        "recency",
        "provenance",
        "node_validity",
        "path",
    }


def test_v2f_hop_and_relation_filters_bound_expansion(session):
    world = _seed_world(session, suffix="filters")

    seeds_only = compile_graph_context(
        session,
        DEFAULT_TENANT_ID,
        "Ângelo",
        max_hops=0,
        item_budget=20,
        token_budget=6000,
    )
    assert seeds_only.items
    assert all(item.hop_count == 0 for item in seeds_only.items)
    assert not any(
        item.source_id == world["resource"].id
        for item in seeds_only.items
    )

    explicit_only = compile_graph_context(
        session,
        DEFAULT_TENANT_ID,
        "Ângelo",
        max_hops=2,
        item_budget=20,
        token_budget=6000,
        relation_filters=(CognitiveRelationKind.EXPLICIT_RELATION,),
    )
    assert any(
        item.source_id == world["resource"].id
        for item in explicit_only.items
    )
    assert not any(
        item.source_id in {
            world["recent"].id,
            world["old"].id,
            world["fact"].id,
            world["state"].id,
        }
        for item in explicit_only.items
    )


def test_v2f_recency_weights_related_events(session):
    world = _seed_world(session, suffix="recency")

    packet = compile_graph_context(
        session,
        DEFAULT_TENANT_ID,
        "Casa 07 recency",
        max_hops=1,
        item_budget=20,
        token_budget=6000,
    )

    recent = _item_by_source(packet, world["recent"].id)
    old = _item_by_source(packet, world["old"].id)
    assert recent.hop_count == old.hop_count == 1
    assert recent.score_components["recency"] > old.score_components["recency"]
    assert recent.relevance_score > old.relevance_score


def test_v2f_secret_timeline_events_are_fail_closed(session):
    world = _seed_world(session, suffix="secret")

    graph = build_cognitive_graph_slice(session, DEFAULT_TENANT_ID)
    assert f"event:{world['secret'].id}" not in graph.node_index()

    packet = compile_graph_context(
        session,
        DEFAULT_TENANT_ID,
        "CONFIDENTIAL_ACTIVITY",
        max_hops=2,
        item_budget=20,
        token_budget=6000,
    )
    assert all(item.source_id != world["secret"].id for item in packet.items)
    assert all(
        str(item.attributes.get("visibility", "")).upper() != "SECRET"
        for item in packet.items
    )


def test_v2f_falls_back_to_lexical_v0_when_graph_has_no_seed(session):
    world = _seed_world(session, suffix="fallback")
    _claim(
        session,
        world["owner_actor"],
        predicate="profile.favorite_code",
        value="codezxcvunique",
    )

    packet = compile_graph_context(
        session,
        DEFAULT_TENANT_ID,
        "codezxcvunique",
        item_budget=8,
        token_budget=3000,
    )

    assert packet.retrieval_method == "LEXICAL_FALLBACK_V0"
    assert packet.lexical_fallback_used is True
    assert packet.seed_node_ids == ()
    assert len(packet.items) == 1
    assert packet.items[0].label == "profile.favorite_code"
    assert packet.items[0].attributes["value_text"] == "codezxcvunique"


def test_v2f_item_and_token_budgets_are_hard(session):
    _seed_world(session, suffix="budget")

    packet = compile_graph_context(
        session,
        DEFAULT_TENANT_ID,
        "Ângelo",
        max_hops=3,
        item_budget=2,
        token_budget=1200,
    )

    assert len(packet.items) <= 2
    assert packet.estimated_tokens <= 1200

    with pytest.raises(
        ContextCompilerError,
        match="CONTEXT_COMPILER_HOPS_OUT_OF_RANGE",
    ):
        compile_graph_context(
            session,
            DEFAULT_TENANT_ID,
            "Ângelo",
            max_hops=5,
        )

    with pytest.raises(
        ContextCompilerError,
        match="CONTEXT_COMPILER_TOKEN_BUDGET_OUT_OF_RANGE",
    ):
        compile_graph_context(
            session,
            DEFAULT_TENANT_ID,
            "Ângelo",
            token_budget=31,
        )


def test_v2f_is_tenant_scoped(session):
    tenant_b = "00000000-0000-4000-8000-00000000f225"
    world_a = _seed_world(session, suffix="tenant-a")
    world_b = _seed_world(
        session,
        tenant_id=tenant_b,
        suffix="tenant-b",
    )

    packet_a = compile_graph_context(
        session,
        DEFAULT_TENANT_ID,
        "Ângelo",
        max_hops=2,
        item_budget=30,
        token_budget=7000,
    )

    assert any(
        item.source_id == world_a["resource"].id
        for item in packet_a.items
    )
    foreign_ids = {
        world_b["resource"].id,
        world_b["relationship"].id,
        world_b["recent"].id,
        world_b["old"].id,
        world_b["fact"].id,
        world_b["state"].id,
        world_b["episode"].id,
    }
    assert not any(item.source_id in foreign_ids for item in packet_a.items)


def test_v2f_compilation_creates_zero_authority_or_outbound_side_effects(session):
    _seed_world(session, suffix="safe")

    before = {
        "execution": session.scalar(
            select(func.count()).select_from(ExecutionIntentRow)
        ),
        "outbox": session.scalar(
            select(func.count()).select_from(OutboxMessageRow)
        ),
    }

    packet = compile_graph_context(
        session,
        DEFAULT_TENANT_ID,
        "Ângelo",
        max_hops=2,
        item_budget=12,
        token_budget=3000,
    )

    after = {
        "execution": session.scalar(
            select(func.count()).select_from(ExecutionIntentRow)
        ),
        "outbox": session.scalar(
            select(func.count()).select_from(OutboxMessageRow)
        ),
    }
    assert packet.items
    assert before == after
