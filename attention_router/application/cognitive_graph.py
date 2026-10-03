"""Read-only Personal Context V2A Cognitive Graph projection.

The projection reuses existing PostgreSQL source-of-truth rows.  It performs no
semantic inference and creates no database rows.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.personal_context_controls import (
    CONTEXT_CONTROL_PREDICATE,
    claim_is_owner_private,
)
from attention_router.domain.cognitive_graph import (
    CognitiveEdge,
    CognitiveGraphSlice,
    CognitiveInferenceClass,
    CognitiveNode,
    CognitiveNodeKind,
    CognitiveRelationKind,
)
from attention_router.infrastructure.entity_resolution_models import (
    EntityAliasResolutionRow,
)
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.semantic_episode_models import (
    SemanticEpisodeMembershipRow,
    SemanticEpisodeRow,
)
from attention_router.infrastructure.obligation_models import (
    ObligationFulfillmentRow,
    ObligationInstanceRow,
    RecurringObligationDefinitionRow,
)
from attention_router.infrastructure.models import (
    ActorBindingRow,
    ConversationMessageRow,
    EntityStateRow,
    FactRow,
    MemoryActorRow,
    MemoryClaimRow,
    RelationshipRow,
    ResourceRow,
    TenantRow,
    TimelineEventRow,
)


class CognitiveGraphUnavailable(RuntimeError):
    pass


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _person_node_id(actor_key: str) -> str:
    return f"person:{actor_key}"


def _resource_node_id(resource_id: str) -> str:
    return f"resource:{resource_id}"


def _relationship_node_id(relationship_id: str) -> str:
    return f"relationship:{relationship_id}"


def _event_node_id(event_id: str) -> str:
    return f"event:{event_id}"


def _claim_node_id(claim_id: str) -> str:
    return f"claim:{claim_id}"


def _fact_node_id(fact_id: str) -> str:
    return f"fact:{fact_id}"


def _state_node_id(state_id: str) -> str:
    return f"state:{state_id}"


def _episode_node_id(episode_id: str) -> str:
    return f"episode:{episode_id}"


def _message_node_id(message_id: str) -> str:
    return f"message:{message_id}"


def _obligation_node_id(definition_id: str) -> str:
    return f"obligation:{definition_id}"


def _expectation_node_id(instance_id: str) -> str:
    return f"expectation:{instance_id}"


def _entity_node_id(entity_type: str, entity_id: str) -> tuple[str, CognitiveNodeKind]:
    normalized = entity_type.strip().upper()
    if normalized in {"ACTOR", "PERSON", "HUMAN", "HUMAN_IDENTITY"}:
        return _person_node_id(entity_id), CognitiveNodeKind.PERSON
    if normalized == "RESOURCE":
        return _resource_node_id(entity_id), CognitiveNodeKind.RESOURCE
    return f"entity:{normalized.casefold()}:{entity_id}", CognitiveNodeKind.ENTITY


def _edge_id(
    *,
    tenant_id: str,
    source_node_id: str,
    target_node_id: str,
    relation_kind: CognitiveRelationKind,
    semantic_relation: str | None = None,
    source_ref: str | None = None,
) -> str:
    digest = stable_hash(
        {
            "tenant_id": tenant_id,
            "source": source_node_id,
            "target": target_node_id,
            "relation_kind": relation_kind.value,
            "semantic_relation": semantic_relation,
            "source_ref": source_ref,
        }
    )[:24]
    return f"cg-edge:{digest}"


def _placeholder_node(
    *,
    tenant_id: str,
    entity_type: str,
    entity_id: str,
) -> CognitiveNode:
    node_id, kind = _entity_node_id(entity_type, entity_id)
    return CognitiveNode(
        node_id=node_id,
        tenant_id=tenant_id,
        kind=kind,
        source_type="ENTITY_REFERENCE",
        source_id=entity_id,
        attributes={"entity_type": entity_type.strip().upper()},
        provenance={"projection": "V2A_REFERENCE"},
    )


def build_cognitive_graph_slice(
    session: Session,
    tenant_id: str,
    *,
    limit_per_kind: int = 200,
    include_secret: bool = False,
    now: datetime | None = None,
) -> CognitiveGraphSlice:
    """Project one bounded tenant graph without mutating source-of-truth rows."""
    if session.get(TenantRow, tenant_id) is None:
        raise CognitiveGraphUnavailable("COGNITIVE_GRAPH_TENANT_NOT_FOUND")
    if limit_per_kind < 1 or limit_per_kind > 1000:
        raise ValueError("COGNITIVE_GRAPH_LIMIT_OUT_OF_RANGE")

    stamp = _utc(now or datetime.now(UTC))
    assert stamp is not None

    nodes: dict[str, CognitiveNode] = {}
    edges: dict[str, CognitiveEdge] = {}

    def put_node(node: CognitiveNode) -> None:
        if node.tenant_id != tenant_id:
            raise CognitiveGraphUnavailable("COGNITIVE_GRAPH_CROSS_TENANT_NODE")
        current = nodes.get(node.node_id)
        if current is None:
            nodes[node.node_id] = node
            return
        if current.label is None and node.label is not None:
            nodes[node.node_id] = replace(current, label=node.label)

    def ensure_entity(entity_type: str, entity_id: str) -> str:
        placeholder = _placeholder_node(
            tenant_id=tenant_id,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        put_node(placeholder)
        return placeholder.node_id

    def put_edge(edge: CognitiveEdge) -> None:
        if edge.tenant_id != tenant_id:
            raise CognitiveGraphUnavailable("COGNITIVE_GRAPH_CROSS_TENANT_EDGE")
        edges.setdefault(edge.edge_id, edge)

    bindings = list(
        session.scalars(
            select(ActorBindingRow)
            .where(ActorBindingRow.tenant_id == tenant_id)
            .order_by(ActorBindingRow.updated_at.desc(), ActorBindingRow.id)
            .limit(limit_per_kind)
        ).all()
    )
    memory_actors = list(
        session.scalars(
            select(MemoryActorRow)
            .where(MemoryActorRow.tenant_id == tenant_id)
            .order_by(MemoryActorRow.updated_at.desc(), MemoryActorRow.id)
            .limit(limit_per_kind)
        ).all()
    )

    binding_groups: dict[str, list[ActorBindingRow]] = {}
    for row in bindings:
        binding_groups.setdefault(row.actor_key, []).append(row)
    memory_groups: dict[str, list[MemoryActorRow]] = {}
    memory_actor_key_by_id: dict[str, str] = {}
    for row in memory_actors:
        memory_groups.setdefault(row.actor_key, []).append(row)
        memory_actor_key_by_id[row.id] = row.actor_key

    for actor_key in sorted(set(binding_groups) | set(memory_groups)):
        actor_bindings = binding_groups.get(actor_key, [])
        actor_memories = memory_groups.get(actor_key, [])
        label = next(
            (
                row.display_name
                for row in actor_bindings
                if isinstance(row.display_name, str) and row.display_name.strip()
            ),
            None,
        )
        put_node(
            CognitiveNode(
                node_id=_person_node_id(actor_key),
                tenant_id=tenant_id,
                kind=CognitiveNodeKind.PERSON,
                source_type="ACTOR_IDENTITY",
                source_id=actor_key,
                label=label,
                attributes={
                    "actor_categories": sorted(
                        {row.actor_category for row in actor_bindings}
                    ),
                    "active": any(row.is_active for row in actor_bindings)
                    if actor_bindings
                    else True,
                },
                provenance={
                    "actor_binding_ids": sorted(row.id for row in actor_bindings),
                    "memory_actor_ids": sorted(row.id for row in actor_memories),
                    "sources": sorted({row.source for row in actor_bindings}),
                },
            )
        )

    resources = list(
        session.scalars(
            select(ResourceRow)
            .where(ResourceRow.tenant_id == tenant_id)
            .order_by(ResourceRow.updated_at.desc(), ResourceRow.id)
            .limit(limit_per_kind)
        ).all()
    )
    for row in resources:
        put_node(
            CognitiveNode(
                node_id=_resource_node_id(row.id),
                tenant_id=tenant_id,
                kind=CognitiveNodeKind.RESOURCE,
                source_type="RESOURCE",
                source_id=row.id,
                label=row.canonical_name,
                attributes={
                    "resource_type": row.resource_type,
                    "status": row.status,
                },
                provenance={"source_table": "resources"},
            )
        )

    relationships = list(
        session.scalars(
            select(RelationshipRow)
            .where(RelationshipRow.tenant_id == tenant_id)
            .order_by(RelationshipRow.updated_at.desc(), RelationshipRow.id)
            .limit(limit_per_kind)
        ).all()
    )
    for row in relationships:
        relation_node_id = _relationship_node_id(row.id)
        put_node(
            CognitiveNode(
                node_id=relation_node_id,
                tenant_id=tenant_id,
                kind=CognitiveNodeKind.RELATIONSHIP,
                source_type="RELATIONSHIP",
                source_id=row.id,
                label=row.relationship_type,
                valid_from=_utc(row.valid_from),
                valid_until=_utc(row.valid_until),
                attributes={"status": row.status},
                provenance={"source_table": "relationships"},
            )
        )
        source_node_id = ensure_entity(
            row.source_entity_type,
            row.source_entity_id,
        )
        target_node_id = ensure_entity(
            row.target_entity_type,
            row.target_entity_id,
        )
        for relation_kind, source_id, target_id in (
            (
                CognitiveRelationKind.RELATIONSHIP_SOURCE,
                source_node_id,
                relation_node_id,
            ),
            (
                CognitiveRelationKind.RELATIONSHIP_TARGET,
                relation_node_id,
                target_node_id,
            ),
        ):
            edge = CognitiveEdge(
                edge_id=_edge_id(
                    tenant_id=tenant_id,
                    source_node_id=source_id,
                    target_node_id=target_id,
                    relation_kind=relation_kind,
                    source_ref=row.id,
                ),
                tenant_id=tenant_id,
                source_node_id=source_id,
                target_node_id=target_id,
                relation_kind=relation_kind,
                inference_class=CognitiveInferenceClass.STRUCTURAL_PROJECTION,
                valid_from=_utc(row.valid_from),
                valid_until=_utc(row.valid_until),
                provenance={"relationship_id": row.id},
            )
            put_edge(edge)

        direct = CognitiveEdge(
            edge_id=_edge_id(
                tenant_id=tenant_id,
                source_node_id=source_node_id,
                target_node_id=target_node_id,
                relation_kind=CognitiveRelationKind.EXPLICIT_RELATION,
                semantic_relation=row.relationship_type,
                source_ref=row.id,
            ),
            tenant_id=tenant_id,
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            relation_kind=CognitiveRelationKind.EXPLICIT_RELATION,
            semantic_relation=row.relationship_type,
            inference_class=CognitiveInferenceClass.EXPLICIT,
            valid_from=_utc(row.valid_from),
            valid_until=_utc(row.valid_until),
            provenance={"relationship_id": row.id},
        )
        put_edge(direct)

    timeline_query = (
        select(TimelineEventRow)
        .where(TimelineEventRow.tenant_id == tenant_id)
        .order_by(TimelineEventRow.occurred_at.desc(), TimelineEventRow.id)
        .limit(limit_per_kind)
    )
    if not include_secret:
        timeline_query = timeline_query.where(
            TimelineEventRow.visibility != "SECRET"
        )
    timeline_events = list(session.scalars(timeline_query).all())
    for row in timeline_events:
        event_node_id = _event_node_id(row.id)
        put_node(
            CognitiveNode(
                node_id=event_node_id,
                tenant_id=tenant_id,
                kind=CognitiveNodeKind.EVENT,
                source_type="TIMELINE_EVENT",
                source_id=row.id,
                valid_from=_utc(row.occurred_at),
                attributes={
                    "event_type": row.event_type,
                    "visibility": row.visibility,
                },
                provenance={
                    "source_table": "timeline_events",
                    "provenance": row.provenance,
                    "canonical_event_id": row.canonical_event_id,
                },
            )
        )
        attachments: list[tuple[CognitiveRelationKind, str]] = []
        if row.actor_id:
            attachments.append(
                (
                    CognitiveRelationKind.EVENT_ACTOR,
                    ensure_entity("ACTOR", row.actor_id),
                )
            )
        if row.resource_id:
            attachments.append(
                (
                    CognitiveRelationKind.EVENT_RESOURCE,
                    ensure_entity("RESOURCE", row.resource_id),
                )
            )
        if row.relationship_id:
            relationship_node_id = _relationship_node_id(row.relationship_id)
            if relationship_node_id not in nodes:
                put_node(
                    CognitiveNode(
                        node_id=relationship_node_id,
                        tenant_id=tenant_id,
                        kind=CognitiveNodeKind.RELATIONSHIP,
                        source_type="RELATIONSHIP_REFERENCE",
                        source_id=row.relationship_id,
                        provenance={"projection": "V2A_REFERENCE"},
                    )
                )
            attachments.append(
                (
                    CognitiveRelationKind.EVENT_RELATIONSHIP,
                    relationship_node_id,
                )
            )
        for relation_kind, target_node_id in attachments:
            put_edge(
                CognitiveEdge(
                    edge_id=_edge_id(
                        tenant_id=tenant_id,
                        source_node_id=event_node_id,
                        target_node_id=target_node_id,
                        relation_kind=relation_kind,
                        source_ref=row.id,
                    ),
                    tenant_id=tenant_id,
                    source_node_id=event_node_id,
                    target_node_id=target_node_id,
                    relation_kind=relation_kind,
                    inference_class=CognitiveInferenceClass.STRUCTURAL_PROJECTION,
                    provenance={"timeline_event_id": row.id},
                )
            )

    claim_query = (
        select(MemoryClaimRow)
        .join(
            MemoryActorRow,
            MemoryActorRow.id == MemoryClaimRow.subject_actor_id,
        )
        .where(
            MemoryActorRow.tenant_id == tenant_id,
            MemoryClaimRow.status == "ACTIVE",
        )
        .order_by(MemoryClaimRow.updated_at.desc(), MemoryClaimRow.id)
        .limit(limit_per_kind)
    )
    if not include_secret:
        claim_query = claim_query.where(
            MemoryClaimRow.sensitivity_class != "SECRET"
        )
    claims = [
        row
        for row in session.scalars(claim_query).all()
        if row.predicate != CONTEXT_CONTROL_PREDICATE
        and not claim_is_owner_private(session, claim=row)
    ]

    for row in claims:
        claim_node_id = _claim_node_id(row.id)
        put_node(
            CognitiveNode(
                node_id=claim_node_id,
                tenant_id=tenant_id,
                kind=CognitiveNodeKind.CLAIM,
                source_type="MEMORY_CLAIM",
                source_id=row.id,
                label=row.predicate,
                confidence=row.confidence,
                valid_from=_utc(row.valid_from),
                valid_until=_utc(row.valid_until),
                attributes={
                    "predicate": row.predicate,
                    "object_type": row.object_type,
                    "status": row.status,
                    "staleness_class": row.staleness_class,
                    "sensitivity_class": row.sensitivity_class,
                    "source_quality": row.source_quality,
                },
                provenance={
                    "source_table": "memory_claims",
                    "supersedes_claim_id": row.supersedes_claim_id,
                    "conflict_group_id": row.conflict_group_id,
                    "first_observed_at": _utc(row.first_observed_at),
                    "last_observed_at": _utc(row.last_observed_at),
                },
            )
        )
        subject_key = memory_actor_key_by_id.get(row.subject_actor_id or "")
        if subject_key:
            subject_node_id = ensure_entity("ACTOR", subject_key)
            put_edge(
                CognitiveEdge(
                    edge_id=_edge_id(
                        tenant_id=tenant_id,
                        source_node_id=subject_node_id,
                        target_node_id=claim_node_id,
                        relation_kind=CognitiveRelationKind.CLAIM_SUBJECT,
                        source_ref=row.id,
                    ),
                    tenant_id=tenant_id,
                    source_node_id=subject_node_id,
                    target_node_id=claim_node_id,
                    relation_kind=CognitiveRelationKind.CLAIM_SUBJECT,
                    inference_class=CognitiveInferenceClass.STRUCTURAL_PROJECTION,
                    confidence=row.confidence,
                    provenance={"claim_id": row.id},
                )
            )
        elif row.subject_entity_id:
            subject_node_id = ensure_entity("ENTITY", row.subject_entity_id)
            put_edge(
                CognitiveEdge(
                    edge_id=_edge_id(
                        tenant_id=tenant_id,
                        source_node_id=subject_node_id,
                        target_node_id=claim_node_id,
                        relation_kind=CognitiveRelationKind.CLAIM_SUBJECT,
                        source_ref=row.id,
                    ),
                    tenant_id=tenant_id,
                    source_node_id=subject_node_id,
                    target_node_id=claim_node_id,
                    relation_kind=CognitiveRelationKind.CLAIM_SUBJECT,
                    inference_class=CognitiveInferenceClass.STRUCTURAL_PROJECTION,
                    confidence=row.confidence,
                    provenance={"claim_id": row.id},
                )
            )

        object_key = memory_actor_key_by_id.get(row.object_actor_id or "")
        object_node_id: str | None = None
        if object_key:
            object_node_id = ensure_entity("ACTOR", object_key)
        elif row.object_entity_id:
            object_node_id = ensure_entity("ENTITY", row.object_entity_id)
        if object_node_id:
            put_edge(
                CognitiveEdge(
                    edge_id=_edge_id(
                        tenant_id=tenant_id,
                        source_node_id=claim_node_id,
                        target_node_id=object_node_id,
                        relation_kind=CognitiveRelationKind.CLAIM_OBJECT,
                        source_ref=row.id,
                    ),
                    tenant_id=tenant_id,
                    source_node_id=claim_node_id,
                    target_node_id=object_node_id,
                    relation_kind=CognitiveRelationKind.CLAIM_OBJECT,
                    inference_class=CognitiveInferenceClass.STRUCTURAL_PROJECTION,
                    confidence=row.confidence,
                    provenance={"claim_id": row.id},
                )
            )

    facts = list(
        session.scalars(
            select(FactRow)
            .where(FactRow.tenant_id == tenant_id)
            .order_by(FactRow.observed_at.desc(), FactRow.id)
            .limit(limit_per_kind)
        ).all()
    )
    for row in facts:
        fact_node_id = _fact_node_id(row.id)
        put_node(
            CognitiveNode(
                node_id=fact_node_id,
                tenant_id=tenant_id,
                kind=CognitiveNodeKind.FACT,
                source_type="FACT",
                source_id=row.id,
                label=row.predicate,
                confidence=row.confidence,
                valid_from=_utc(row.valid_from),
                valid_until=_utc(row.valid_until),
                attributes={
                    "predicate": row.predicate,
                    "fact_class": row.fact_class,
                    "source_type": row.source_type,
                },
                provenance={
                    "source_table": "facts",
                    "source_ref": row.source_ref,
                    "observed_at": _utc(row.observed_at),
                    "supersedes_fact_id": row.supersedes_fact_id,
                },
            )
        )
        subject_node_id = ensure_entity(row.subject_type, row.subject_id)
        put_edge(
            CognitiveEdge(
                edge_id=_edge_id(
                    tenant_id=tenant_id,
                    source_node_id=subject_node_id,
                    target_node_id=fact_node_id,
                    relation_kind=CognitiveRelationKind.FACT_SUBJECT,
                    source_ref=row.id,
                ),
                tenant_id=tenant_id,
                source_node_id=subject_node_id,
                target_node_id=fact_node_id,
                relation_kind=CognitiveRelationKind.FACT_SUBJECT,
                inference_class=CognitiveInferenceClass.STRUCTURAL_PROJECTION,
                confidence=row.confidence,
                provenance={"fact_id": row.id},
            )
        )

    states = list(
        session.scalars(
            select(EntityStateRow)
            .where(EntityStateRow.tenant_id == tenant_id)
            .order_by(EntityStateRow.updated_at.desc(), EntityStateRow.id)
            .limit(limit_per_kind)
        ).all()
    )
    for row in states:
        state_node_id = _state_node_id(row.id)
        put_node(
            CognitiveNode(
                node_id=state_node_id,
                tenant_id=tenant_id,
                kind=CognitiveNodeKind.STATE,
                source_type="ENTITY_STATE",
                source_id=row.id,
                label=f"{row.state_namespace}.{row.state_key}",
                valid_from=_utc(row.effective_at),
                valid_until=_utc(row.expires_at),
                attributes={
                    "state_namespace": row.state_namespace,
                    "state_key": row.state_key,
                    "version": row.version,
                },
                provenance={
                    "source_table": "entity_states",
                    "source": row.source,
                },
            )
        )
        subject_node_id = ensure_entity(row.subject_type, row.subject_id)
        put_edge(
            CognitiveEdge(
                edge_id=_edge_id(
                    tenant_id=tenant_id,
                    source_node_id=subject_node_id,
                    target_node_id=state_node_id,
                    relation_kind=CognitiveRelationKind.STATE_SUBJECT,
                    source_ref=row.id,
                ),
                tenant_id=tenant_id,
                source_node_id=subject_node_id,
                target_node_id=state_node_id,
                relation_kind=CognitiveRelationKind.STATE_SUBJECT,
                inference_class=CognitiveInferenceClass.STRUCTURAL_PROJECTION,
                provenance={"state_id": row.id},
            )
        )


    aliases = list(
        session.scalars(
            select(EntityAliasResolutionRow)
            .where(
                EntityAliasResolutionRow.tenant_id == tenant_id,
                EntityAliasResolutionRow.state == "ACTIVE",
            )
            .order_by(
                EntityAliasResolutionRow.updated_at.desc(),
                EntityAliasResolutionRow.id,
            )
            .limit(limit_per_kind)
        ).all()
    )
    for row in aliases:
        alias_node_id = ensure_entity("ACTOR", row.alias_actor_key)
        canonical_node_id = ensure_entity("ACTOR", row.canonical_actor_key)
        put_edge(
            CognitiveEdge(
                edge_id=_edge_id(
                    tenant_id=tenant_id,
                    source_node_id=alias_node_id,
                    target_node_id=canonical_node_id,
                    relation_kind=CognitiveRelationKind.IDENTITY_ALIAS,
                    semantic_relation="ALIAS_OF",
                    source_ref=row.id,
                ),
                tenant_id=tenant_id,
                source_node_id=alias_node_id,
                target_node_id=canonical_node_id,
                relation_kind=CognitiveRelationKind.IDENTITY_ALIAS,
                semantic_relation="ALIAS_OF",
                inference_class=CognitiveInferenceClass.EXPLICIT,
                confidence=1.0,
                provenance={
                    "alias_resolution_id": row.id,
                    "candidate_id": row.candidate_id,
                    "decision_actor_key": row.decision_actor_key,
                    "decision_ref": row.decision_ref,
                },
            )
        )


    episode_query = (
        select(SemanticEpisodeRow)
        .where(SemanticEpisodeRow.tenant_id == tenant_id)
        .order_by(
            SemanticEpisodeRow.last_activity_at.desc(),
            SemanticEpisodeRow.id,
        )
        .limit(limit_per_kind)
    )
    if not include_secret:
        episode_query = episode_query.where(
            SemanticEpisodeRow.sensitivity_class != "SECRET"
        )
    episodes = list(session.scalars(episode_query).all())
    episode_ids = {row.id for row in episodes}

    for row in episodes:
        episode_node_id = _episode_node_id(row.id)
        put_node(
            CognitiveNode(
                node_id=episode_node_id,
                tenant_id=tenant_id,
                kind=CognitiveNodeKind.EPISODE,
                source_type="SEMANTIC_EPISODE",
                source_id=row.id,
                label=row.episode_type,
                confidence=row.confidence,
                valid_from=_utc(row.started_at),
                valid_until=_utc(row.ended_at),
                attributes={
                    "episode_type": row.episode_type,
                    "scope_type": row.scope_type,
                    "scope_ref": row.scope_ref,
                    "state": row.state,
                    "sensitivity_class": row.sensitivity_class,
                    "last_activity_at": _utc(row.last_activity_at),
                },
                provenance={
                    "source_table": "semantic_episodes",
                    "semantic_key": row.semantic_key,
                    "supersedes_episode_id": row.supersedes_episode_id,
                    "split_from_episode_id": row.split_from_episode_id,
                    "merged_from_episode_ids": row.merged_from_episode_ids,
                    "provenance": row.provenance,
                },
            )
        )
        if row.scope_type == "RESOURCE":
            target_node_id = ensure_entity("RESOURCE", row.scope_ref)
            relation_kind = CognitiveRelationKind.EPISODE_RESOURCE
        elif row.scope_type == "RELATIONSHIP":
            target_node_id = _relationship_node_id(row.scope_ref)
            if target_node_id not in nodes:
                put_node(
                    CognitiveNode(
                        node_id=target_node_id,
                        tenant_id=tenant_id,
                        kind=CognitiveNodeKind.RELATIONSHIP,
                        source_type="RELATIONSHIP_REFERENCE",
                        source_id=row.scope_ref,
                        provenance={"projection": "V2D_REFERENCE"},
                    )
                )
            relation_kind = CognitiveRelationKind.EPISODE_RELATIONSHIP
        else:
            target_node_id = None
            relation_kind = None

        if target_node_id is not None and relation_kind is not None:
            put_edge(
                CognitiveEdge(
                    edge_id=_edge_id(
                        tenant_id=tenant_id,
                        source_node_id=episode_node_id,
                        target_node_id=target_node_id,
                        relation_kind=relation_kind,
                        source_ref=row.id,
                    ),
                    tenant_id=tenant_id,
                    source_node_id=episode_node_id,
                    target_node_id=target_node_id,
                    relation_kind=relation_kind,
                    inference_class=CognitiveInferenceClass.STRUCTURAL_PROJECTION,
                    confidence=row.confidence,
                    provenance={"episode_id": row.id},
                )
            )

    if episode_ids:
        memberships = list(
            session.scalars(
                select(SemanticEpisodeMembershipRow)
                .where(
                    SemanticEpisodeMembershipRow.tenant_id == tenant_id,
                    SemanticEpisodeMembershipRow.episode_id.in_(episode_ids),
                    SemanticEpisodeMembershipRow.ambiguous.is_(False),
                )
                .order_by(
                    SemanticEpisodeMembershipRow.observed_at.desc(),
                    SemanticEpisodeMembershipRow.id,
                )
                .limit(limit_per_kind * 5)
            ).all()
        )
    else:
        memberships = []

    for row in memberships:
        episode_node_id = _episode_node_id(row.episode_id)
        if episode_node_id not in nodes:
            continue

        target_node_id: str | None = None
        if row.member_type == "TIMELINE_EVENT":
            target_node_id = _event_node_id(row.member_ref)
            if target_node_id not in nodes:
                event = session.get(TimelineEventRow, row.member_ref)
                if event is None or event.tenant_id != tenant_id:
                    continue
                if not include_secret and event.visibility == "SECRET":
                    continue
                put_node(
                    CognitiveNode(
                        node_id=target_node_id,
                        tenant_id=tenant_id,
                        kind=CognitiveNodeKind.EVENT,
                        source_type="TIMELINE_EVENT_REFERENCE",
                        source_id=event.id,
                        label=event.event_type,
                        valid_from=_utc(event.occurred_at),
                        provenance={"projection": "V2D_REFERENCE"},
                    )
                )
        elif row.member_type == "CONVERSATION_MESSAGE":
            message = session.get(ConversationMessageRow, row.member_ref)
            if message is None or message.tenant_id != tenant_id:
                continue
            if not include_secret and message.sensitivity_class == "SECRET":
                continue
            target_node_id = _message_node_id(message.id)
            put_node(
                CognitiveNode(
                    node_id=target_node_id,
                    tenant_id=tenant_id,
                    kind=CognitiveNodeKind.MESSAGE,
                    source_type="CONVERSATION_MESSAGE",
                    source_id=message.id,
                    valid_from=_utc(message.sent_at),
                    attributes={
                        "message_type": message.message_type,
                        "direction": message.direction,
                        "sensitivity_class": message.sensitivity_class,
                    },
                    provenance={
                        "source": message.source,
                        "source_account": message.source_account,
                        "conversation_id": message.conversation_id,
                    },
                )
            )

        if target_node_id is None:
            continue
        put_edge(
            CognitiveEdge(
                edge_id=_edge_id(
                    tenant_id=tenant_id,
                    source_node_id=episode_node_id,
                    target_node_id=target_node_id,
                    relation_kind=CognitiveRelationKind.EPISODE_MEMBER,
                    semantic_relation=row.association_reason,
                    source_ref=row.id,
                ),
                tenant_id=tenant_id,
                source_node_id=episode_node_id,
                target_node_id=target_node_id,
                relation_kind=CognitiveRelationKind.EPISODE_MEMBER,
                semantic_relation=row.association_reason,
                inference_class=CognitiveInferenceClass.EXPLICIT,
                confidence=row.confidence,
                provenance={
                    "episode_membership_id": row.id,
                    "association_source": row.association_source,
                    "observed_at": _utc(row.observed_at),
                },
            )
        )


    obligation_query = (
        select(RecurringObligationDefinitionRow)
        .where(RecurringObligationDefinitionRow.tenant_id == tenant_id)
        .order_by(
            RecurringObligationDefinitionRow.updated_at.desc(),
            RecurringObligationDefinitionRow.id,
        )
        .limit(limit_per_kind)
    )
    if not include_secret:
        obligation_query = obligation_query.where(
            RecurringObligationDefinitionRow.sensitivity_class != "SECRET"
        )
    obligation_definitions = list(session.scalars(obligation_query).all())
    obligation_ids = {row.id for row in obligation_definitions}

    for row in obligation_definitions:
        obligation_node_id = _obligation_node_id(row.id)
        put_node(
            CognitiveNode(
                node_id=obligation_node_id,
                tenant_id=tenant_id,
                kind=CognitiveNodeKind.OBLIGATION,
                source_type="RECURRING_OBLIGATION",
                source_id=row.id,
                label=row.obligation_kind,
                confidence=row.confidence,
                valid_from=_utc(row.valid_from),
                valid_until=_utc(row.valid_until),
                attributes={
                    "obligation_kind": row.obligation_kind,
                    "expected_actor_key": row.expected_actor_key,
                    "expected_event_type": row.expected_event_type,
                    "cadence_kind": row.cadence_kind,
                    "due_day": row.due_day,
                    "due_timezone": row.due_timezone,
                    "grace_seconds": row.grace_seconds,
                    "state": row.state,
                    "sensitivity_class": row.sensitivity_class,
                    "value_constraints": row.value_constraints,
                },
                provenance={
                    "source_table": "recurring_obligation_definitions",
                    "semantic_key": row.semantic_key,
                    "source_kind": row.source_kind,
                    "source_ref": row.source_ref,
                    "supersedes_definition_id": row.supersedes_definition_id,
                    "provenance": row.provenance,
                },
            )
        )

        subject_node_id = ensure_entity(row.subject_type, row.subject_id)
        put_edge(
            CognitiveEdge(
                edge_id=_edge_id(
                    tenant_id=tenant_id,
                    source_node_id=obligation_node_id,
                    target_node_id=subject_node_id,
                    relation_kind=CognitiveRelationKind.OBLIGATION_SUBJECT,
                    source_ref=row.id,
                ),
                tenant_id=tenant_id,
                source_node_id=obligation_node_id,
                target_node_id=subject_node_id,
                relation_kind=CognitiveRelationKind.OBLIGATION_SUBJECT,
                semantic_relation=row.obligation_kind,
                inference_class=CognitiveInferenceClass.EXPLICIT,
                confidence=row.confidence,
                valid_from=_utc(row.valid_from),
                valid_until=_utc(row.valid_until),
                provenance={"obligation_definition_id": row.id},
            )
        )

        expected_actor_node_id = ensure_entity(
            "ACTOR",
            row.expected_actor_key,
        )
        put_edge(
            CognitiveEdge(
                edge_id=_edge_id(
                    tenant_id=tenant_id,
                    source_node_id=expected_actor_node_id,
                    target_node_id=obligation_node_id,
                    relation_kind=(
                        CognitiveRelationKind.OBLIGATION_EXPECTED_ACTOR
                    ),
                    source_ref=row.id,
                ),
                tenant_id=tenant_id,
                source_node_id=expected_actor_node_id,
                target_node_id=obligation_node_id,
                relation_kind=(
                    CognitiveRelationKind.OBLIGATION_EXPECTED_ACTOR
                ),
                semantic_relation="EXPECTED_ACTOR",
                inference_class=CognitiveInferenceClass.EXPLICIT,
                confidence=row.confidence,
                valid_from=_utc(row.valid_from),
                valid_until=_utc(row.valid_until),
                provenance={"obligation_definition_id": row.id},
            )
        )

    if obligation_ids:
        instance_query = (
            select(ObligationInstanceRow)
            .where(
                ObligationInstanceRow.tenant_id == tenant_id,
                ObligationInstanceRow.definition_id.in_(obligation_ids),
            )
            .order_by(
                ObligationInstanceRow.expected_by.desc(),
                ObligationInstanceRow.id,
            )
            .limit(limit_per_kind * 3)
        )
        if not include_secret:
            instance_query = instance_query.where(
                ObligationInstanceRow.sensitivity_class != "SECRET"
            )
        obligation_instances = list(session.scalars(instance_query).all())
    else:
        obligation_instances = []

    instance_ids = {row.id for row in obligation_instances}
    for row in obligation_instances:
        expectation_node_id = _expectation_node_id(row.id)
        put_node(
            CognitiveNode(
                node_id=expectation_node_id,
                tenant_id=tenant_id,
                kind=CognitiveNodeKind.EXPECTATION,
                source_type="OBLIGATION_INSTANCE",
                source_id=row.id,
                label=row.state,
                confidence=None,
                valid_from=_utc(row.period_start),
                valid_until=_utc(row.period_end),
                attributes={
                    "period_key": row.period_key,
                    "expected_by": _utc(row.expected_by),
                    "due_window_end": _utc(row.due_window_end),
                    "state": row.state,
                    "reconciliation_status": row.reconciliation_status,
                    "uncertainty_code": row.uncertainty_code,
                    "expected_value": row.expected_value,
                    "satisfaction_ratio": row.satisfaction_ratio,
                    "sensitivity_class": row.sensitivity_class,
                    "extension_until": _utc(row.extension_until),
                    "waived_at": _utc(row.waived_at),
                },
                provenance={
                    "source_table": "obligation_instances",
                    "definition_id": row.definition_id,
                    "supersedes_instance_id": row.supersedes_instance_id,
                    "provenance": row.provenance,
                    "absence_is_fact": False,
                },
            )
        )
        obligation_node_id = _obligation_node_id(row.definition_id)
        if obligation_node_id in nodes:
            put_edge(
                CognitiveEdge(
                    edge_id=_edge_id(
                        tenant_id=tenant_id,
                        source_node_id=obligation_node_id,
                        target_node_id=expectation_node_id,
                        relation_kind=(
                            CognitiveRelationKind.EXPECTATION_DEFINITION
                        ),
                        source_ref=row.id,
                    ),
                    tenant_id=tenant_id,
                    source_node_id=obligation_node_id,
                    target_node_id=expectation_node_id,
                    relation_kind=(
                        CognitiveRelationKind.EXPECTATION_DEFINITION
                    ),
                    semantic_relation="HAS_EXPECTATION_INSTANCE",
                    inference_class=(
                        CognitiveInferenceClass.STRUCTURAL_PROJECTION
                    ),
                    provenance={"obligation_instance_id": row.id},
                )
            )

    if instance_ids:
        fulfillments = list(
            session.scalars(
                select(ObligationFulfillmentRow)
                .where(
                    ObligationFulfillmentRow.tenant_id == tenant_id,
                    ObligationFulfillmentRow.instance_id.in_(instance_ids),
                )
                .order_by(
                    ObligationFulfillmentRow.created_at.desc(),
                    ObligationFulfillmentRow.id,
                )
                .limit(limit_per_kind * 5)
            ).all()
        )
    else:
        fulfillments = []

    for row in fulfillments:
        event = session.get(TimelineEventRow, row.timeline_event_id)
        if event is None or event.tenant_id != tenant_id:
            continue
        if not include_secret and event.visibility == "SECRET":
            continue
        event_node_id = _event_node_id(event.id)
        if event_node_id not in nodes:
            put_node(
                CognitiveNode(
                    node_id=event_node_id,
                    tenant_id=tenant_id,
                    kind=CognitiveNodeKind.EVENT,
                    source_type="TIMELINE_EVENT_REFERENCE",
                    source_id=event.id,
                    label=event.event_type,
                    valid_from=_utc(event.occurred_at),
                    attributes={"visibility": event.visibility},
                    provenance={"projection": "V2G_FULFILLMENT_REFERENCE"},
                )
            )
        expectation_node_id = _expectation_node_id(row.instance_id)
        if expectation_node_id not in nodes:
            continue
        put_edge(
            CognitiveEdge(
                edge_id=_edge_id(
                    tenant_id=tenant_id,
                    source_node_id=expectation_node_id,
                    target_node_id=event_node_id,
                    relation_kind=(
                        CognitiveRelationKind.EXPECTATION_FULFILLMENT_EVENT
                    ),
                    source_ref=row.id,
                ),
                tenant_id=tenant_id,
                source_node_id=expectation_node_id,
                target_node_id=event_node_id,
                relation_kind=(
                    CognitiveRelationKind.EXPECTATION_FULFILLMENT_EVENT
                ),
                semantic_relation=row.reconciliation_kind,
                inference_class=CognitiveInferenceClass.EXPLICIT,
                confidence=row.fulfillment_fraction,
                provenance={
                    "obligation_fulfillment_id": row.id,
                    "explicitly_shared": row.explicitly_shared,
                },
            )
        )

    result = CognitiveGraphSlice(
        tenant_id=tenant_id,
        nodes=tuple(sorted(nodes.values(), key=lambda item: item.node_id)),
        edges=tuple(sorted(edges.values(), key=lambda item: item.edge_id)),
        generated_at=stamp,
    )
    result.validate()
    return result


__all__ = [
    "CognitiveGraphUnavailable",
    "build_cognitive_graph_slice",
]
