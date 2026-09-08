from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from attention_router.application.platform.events import event_to_envelope
from attention_router.core.context import ContextCoreBuilder, ContextSnapshot
from attention_router.core.entities import EntityReference
from attention_router.infrastructure.models import (
    AgentDecisionRow,
    CanonicalEventRow,
    CapabilityDefinitionRow,
    CapabilityVersionRow,
    CapabilityGrantRow,
    EntityStateRow,
    FactRow,
    InteractionRow,
    MemoryActorRow,
    MemoryClaimRow,
    TimelineEventRow,
    ActorBindingRow,
)


class DatabaseDecisionRetriever:
    def __init__(self, session: Session):
        self.session = session

    def retrieve(self, tenant_id: str, actor_id: str | None, limit: int) -> list[dict[str, Any]]:
        if not actor_id:
            return []
        rows = self.session.execute(
            select(AgentDecisionRow, InteractionRow)
            .join(InteractionRow, InteractionRow.id == AgentDecisionRow.interaction_id)
            .where(InteractionRow.tenant_id == tenant_id, InteractionRow.contact_id == actor_id)
            .order_by(AgentDecisionRow.created_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "decision_id": decision.id,
                "objective": decision.objective,
                "semantic_source": decision.semantic_source or decision.response_source,
                "status": decision.status,
            }
            for decision, _ in reversed(rows)
        ]


class DatabaseHistoricalContextRetriever:
    def __init__(self, session: Session):
        self.session = session

    def retrieve(self, tenant_id: str, actor_id: str | None, limit: int) -> list[dict[str, Any]]:
        query = select(TimelineEventRow).where(TimelineEventRow.tenant_id == tenant_id)
        if actor_id:
            query = query.where(TimelineEventRow.actor_id == actor_id)
        rows = self.session.scalars(query.order_by(TimelineEventRow.occurred_at.desc()).limit(limit)).all()
        return [
            {
                "event_type": row.event_type,
                "occurred_at": row.occurred_at.isoformat(),
                "provenance": row.provenance,
            }
            for row in reversed(rows)
        ]


class DatabaseMemoryRetriever:
    def __init__(self, session: Session):
        self.session = session

    def retrieve(self, tenant_id: str, actor_id: str | None, limit: int) -> list[dict[str, Any]]:
        if not actor_id:
            return []
        actor = self.session.scalar(
            select(MemoryActorRow).where(
                MemoryActorRow.tenant_id == tenant_id,
                or_(MemoryActorRow.id == actor_id, MemoryActorRow.actor_key == actor_id),
            )
        )
        if actor is None:
            return []
        rows = self.session.scalars(
            select(MemoryClaimRow)
            .where(
                MemoryClaimRow.subject_actor_id == actor.id,
                MemoryClaimRow.status == "ACTIVE",
            )
            .order_by(MemoryClaimRow.updated_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "predicate": row.predicate,
                "object_type": row.object_type,
                "confidence": row.confidence,
                "sensitivity": row.sensitivity_class,
            }
            for row in rows
        ]


class DatabaseStateRetriever:
    def __init__(self, session: Session):
        self.session = session

    def retrieve(self, tenant_id: str, actor_id: str | None, limit: int) -> list[dict[str, Any]]:
        if not actor_id:
            return []
        rows = self.session.scalars(
            select(EntityStateRow)
            .where(
                EntityStateRow.tenant_id == tenant_id,
                EntityStateRow.subject_type == "ACTOR",
                EntityStateRow.subject_id == actor_id,
                EntityStateRow.state_namespace == "presence",
                EntityStateRow.state_key == "effective",
                EntityStateRow.effective_at <= datetime.now(timezone.utc),
                or_(EntityStateRow.expires_at.is_(None), EntityStateRow.expires_at > datetime.now(timezone.utc)),
            )
            .order_by(EntityStateRow.updated_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "namespace": row.state_namespace,
                "key": row.state_key,
                "value": row.state_value,
                "version": row.version,
            }
            for row in rows
        ]


class DatabaseFactRetriever:
    def __init__(self, session: Session):
        self.session = session

    def retrieve(self, tenant_id: str, actor_id: str | None, limit: int) -> list[dict[str, Any]]:
        if not actor_id:
            return []
        rows = self.session.scalars(
            select(FactRow)
            .where(
                FactRow.tenant_id == tenant_id,
                FactRow.subject_type == "ACTOR",
                FactRow.subject_id == actor_id,
            )
            .order_by(FactRow.observed_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "predicate": row.predicate,
                "fact_class": row.fact_class,
                "confidence": row.confidence,
                "value_ref_present": bool(row.value_ref),
            }
            for row in rows
        ]


class DatabaseCapabilityContextProvider:
    def __init__(self, session: Session):
        self.session = session

    def retrieve(self, tenant_id: str, actor_id: str | None, limit: int) -> list[dict[str, Any]]:
        del actor_id
        rows = self.session.scalars(
            select(CapabilityDefinitionRow)
            .where(CapabilityDefinitionRow.tenant_id == tenant_id)
            .order_by(CapabilityDefinitionRow.canonical_name)
            .limit(limit)
        ).all()
        output = []
        for row in rows:
            version = self.session.get(CapabilityVersionRow, row.current_version_id) if row.current_version_id else None
            output.append({
                "capability": row.canonical_name,
                "availability": row.availability_state,
                "operation_type": version.operation_type if version else None,
                "side_effect": version.side_effect if version else None,
            })
        return output


class DatabaseEffectiveAuthorityProvider:
    def __init__(self, session: Session):
        self.session = session

    def retrieve(self, tenant_id: str, actor_id: str | None, limit: int) -> list[dict[str, Any]]:
        if not actor_id:
            return []
        now = datetime.now(timezone.utc)
        rows = self.session.execute(
            select(
                CapabilityGrantRow.status,
                CapabilityGrantRow.valid_until,
                CapabilityDefinitionRow.canonical_name,
            )
            .join(
                CapabilityDefinitionRow,
                CapabilityDefinitionRow.id == CapabilityGrantRow.capability_id,
            )
            .where(
                CapabilityGrantRow.tenant_id == tenant_id,
                CapabilityDefinitionRow.tenant_id == tenant_id,
                CapabilityGrantRow.grantee_type == "ACTOR",
                CapabilityGrantRow.grantee_id == actor_id,
                CapabilityGrantRow.status == "ACTIVE",
                CapabilityGrantRow.valid_from <= now,
                or_(
                    CapabilityGrantRow.valid_until.is_(None),
                    CapabilityGrantRow.valid_until > now,
                ),
            )
            .limit(limit)
        ).all()
        return [
            {
                "capability": capability,
                "grant_status": status,
                "valid_until": valid_until.isoformat() if valid_until else None,
            }
            for status, valid_until, capability in rows
        ]


def build_context_snapshot(
    session: Session,
    event: CanonicalEventRow,
    *,
    limit: int = 20,
    represented_subject: EntityReference | None = None,
) -> ContextSnapshot:
    builder = ContextCoreBuilder(
        historical=DatabaseHistoricalContextRetriever(session),
        memory=DatabaseMemoryRetriever(session),
        state=DatabaseStateRetriever(session),
        facts=DatabaseFactRetriever(session),
        capabilities=DatabaseCapabilityContextProvider(session),
        decisions=DatabaseDecisionRetriever(session),
        authority=DatabaseEffectiveAuthorityProvider(session),
    )
    return builder.build(
        event_to_envelope(event),
        limit=limit,
        state_actor_id=represented_subject.entity_id if represented_subject else None,
    )


def resolve_represented_subject(session: Session, tenant_id: str) -> EntityReference | None:
    """Resolve the single active tenant owner without using transport identifiers."""
    bindings = session.scalars(
        select(ActorBindingRow).where(
            ActorBindingRow.tenant_id == tenant_id,
            ActorBindingRow.is_active.is_(True),
            or_(
                ActorBindingRow.actor_category == "owner",
                ActorBindingRow.binding_metadata["owner"].as_boolean().is_(True),
            ),
        )
    ).all()
    if len(bindings) != 1:
        return None
    return EntityReference(entity_type="ACTOR", entity_id=bindings[0].actor_key)
