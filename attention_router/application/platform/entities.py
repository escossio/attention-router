from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from attention_router.core.entities import EntityReference, FactClass, ResourceSpec
from attention_router.core.tenancy import TenantScopeError
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    ActorBindingRow,
    DeviceRow,
    EntityStateRow,
    FactRow,
    MemoryActorRow,
    RelationshipRow,
    ResourceRow,
)
from attention_router.observability.tracing import safe_set_attribute, set_outcome, start_span
from attention_router.platform.lineage import EventLineage, require_organic_write


@dataclass(frozen=True, slots=True)
class EffectiveRelationship:
    relationship_id: str | None
    relationship_type: str | None
    state: str
    reason_code: str


@dataclass(frozen=True, slots=True)
class EffectiveAudience:
    audience: str | None
    state: str
    reason_code: str


def resolve_effective_relationship(
    session: Session, *, tenant_id: str, source: EntityReference,
    target: EntityReference, now: datetime | None = None,
) -> EffectiveRelationship:
    """Resolve the current tenant-scoped relationship without mutation."""
    timestamp = (now or now_utc()).astimezone(UTC)
    rows = list_relationships(session, tenant_id=tenant_id, entity=source)
    candidates = [row for row in rows if (
        (row.source_entity_type, row.source_entity_id) == (target.entity_type.upper(), target.entity_id)
        or (row.target_entity_type, row.target_entity_id) == (target.entity_type.upper(), target.entity_id)
    ) and (row.valid_from is None or row.valid_from <= timestamp)
       and (row.valid_until is None or row.valid_until > timestamp)
       and row.status == "ACTIVE"]
    if not candidates:
        return EffectiveRelationship(None, None, "UNKNOWN", "RELATIONSHIP_NOT_FOUND")
    candidates.sort(key=lambda row: (row.updated_at, row.id), reverse=True)
    if len(candidates) > 1 and candidates[0].updated_at == candidates[1].updated_at:
        return EffectiveRelationship(None, None, "UNKNOWN", "RELATIONSHIP_AMBIGUOUS")
    row = candidates[0]
    return EffectiveRelationship(row.id, row.relationship_type, "READY", "RELATIONSHIP_RESOLVED")


def resolve_effective_audience(*, actor: ActorBindingRow | None,
                               relationship: EffectiveRelationship) -> EffectiveAudience:
    """Resolve configured audience metadata; never changes classification."""
    if relationship.state != "READY" or actor is None:
        return EffectiveAudience(None, "UNKNOWN", "AUDIENCE_CONTEXT_UNRESOLVED")
    audience = (actor.binding_metadata or {}).get("audience")
    if not isinstance(audience, str) or not audience:
        return EffectiveAudience(None, "UNKNOWN", "AUDIENCE_NOT_CONFIGURED")
    return EffectiveAudience(audience, "READY", "AUDIENCE_RESOLVED")


def create_resource(session: Session, tenant_id: str, spec: ResourceSpec) -> ResourceRow:
    row = ResourceRow(
        id=new_id(),
        tenant_id=tenant_id,
        resource_type=spec.resource_type.upper(),
        canonical_name=spec.canonical_name,
        status="ACTIVE",
        metadata_json=spec.metadata_sanitized,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row


def get_resource(session: Session, tenant_id: str, resource_id: str) -> ResourceRow:
    row = session.get(ResourceRow, resource_id)
    if row is None:
        raise KeyError(resource_id)
    if row.tenant_id != tenant_id:
        raise TenantScopeError("TENANT_SCOPE_MISMATCH:resource")
    return row


def _entity_exists(session: Session, tenant_id: str, ref: EntityReference) -> bool:
    kind = ref.entity_type.upper()
    if kind == "RESOURCE":
        row = session.get(ResourceRow, ref.entity_id)
        return bool(row and row.tenant_id == tenant_id)
    if kind == "DEVICE":
        row = session.get(DeviceRow, ref.entity_id)
        return bool(row and row.tenant_id == tenant_id)
    if kind == "ACTOR":
        binding = session.scalar(
            select(ActorBindingRow).where(
                ActorBindingRow.tenant_id == tenant_id,
                or_(ActorBindingRow.actor_key == ref.entity_id, ActorBindingRow.id == ref.entity_id),
            )
        )
        memory_actor = session.scalar(
            select(MemoryActorRow).where(
                MemoryActorRow.tenant_id == tenant_id,
                or_(MemoryActorRow.actor_key == ref.entity_id, MemoryActorRow.id == ref.entity_id),
            )
        )
        return bool(binding or memory_actor)
    return False


def create_relationship(
    session: Session,
    *,
    tenant_id: str,
    source: EntityReference,
    target: EntityReference,
    relationship_type: str,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    metadata_sanitized: dict[str, Any] | None = None,
    source_lineage: EventLineage | None = None,
) -> RelationshipRow:
    if source_lineage is not None:
        if source_lineage.tenant_id != tenant_id:
            raise TenantScopeError("TENANT_SCOPE_MISMATCH:relationship_lineage")
        require_organic_write(source_lineage, writer="relationship")
    with start_span("relationship.resolve") as span:
        source_exists = _entity_exists(session, tenant_id, source)
        target_exists = _entity_exists(session, tenant_id, target)
        safe_set_attribute(span, "attention.tenant_id", tenant_id)
        safe_set_attribute(span, "attention.relationship_type", relationship_type)
        if not source_exists or not target_exists:
            set_outcome(span, "ENTITY_NOT_FOUND", error=True)
            raise TenantScopeError("RELATIONSHIP_ENTITY_NOT_IN_TENANT")
        row = RelationshipRow(
            id=new_id(),
            tenant_id=tenant_id,
            source_entity_type=source.entity_type.upper(),
            source_entity_id=source.entity_id,
            target_entity_type=target.entity_type.upper(),
            target_entity_id=target.entity_id,
            relationship_type=relationship_type,
            status="ACTIVE",
            valid_from=valid_from,
            valid_until=valid_until,
            metadata_json=metadata_sanitized or {},
            created_at=now_utc(),
            updated_at=now_utc(),
        )
        session.add(row)
        session.flush()
        set_outcome(span, "RESOLVED")
        return row


def list_relationships(
    session: Session,
    *,
    tenant_id: str,
    entity: EntityReference,
) -> list[RelationshipRow]:
    return session.scalars(
        select(RelationshipRow).where(
            RelationshipRow.tenant_id == tenant_id,
            or_(
                (RelationshipRow.source_entity_type == entity.entity_type.upper())
                & (RelationshipRow.source_entity_id == entity.entity_id),
                (RelationshipRow.target_entity_type == entity.entity_type.upper())
                & (RelationshipRow.target_entity_id == entity.entity_id),
            ),
        )
    ).all()


def record_fact(
    session: Session,
    *,
    tenant_id: str,
    subject: EntityReference,
    predicate: str,
    fact_class: FactClass,
    source_type: str,
    confidence: float,
    value: dict[str, Any] | None = None,
    value_ref: str | None = None,
    source_ref: str | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    supersedes_fact_id: str | None = None,
    metadata_sanitized: dict[str, Any] | None = None,
    source_lineage: EventLineage | None = None,
) -> FactRow:
    if source_lineage is not None:
        if source_lineage.tenant_id != tenant_id:
            raise TenantScopeError("TENANT_SCOPE_MISMATCH:fact_lineage")
        require_organic_write(source_lineage, writer="fact")
    if not _entity_exists(session, tenant_id, subject):
        raise TenantScopeError("FACT_SUBJECT_NOT_IN_TENANT")
    if supersedes_fact_id:
        previous = session.get(FactRow, supersedes_fact_id)
        if previous is None or previous.tenant_id != tenant_id:
            raise TenantScopeError("TENANT_SCOPE_MISMATCH:superseded_fact")
    row = FactRow(
        id=new_id(),
        tenant_id=tenant_id,
        subject_type=subject.entity_type.upper(),
        subject_id=subject.entity_id,
        predicate=predicate,
        value_json=value,
        value_ref=value_ref,
        fact_class=fact_class.value,
        source_type=source_type,
        source_ref=source_ref,
        confidence=max(0.0, min(1.0, confidence)),
        observed_at=now_utc(),
        valid_from=valid_from,
        valid_until=valid_until,
        supersedes_fact_id=supersedes_fact_id,
        metadata_json=metadata_sanitized or {},
        created_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row


def set_entity_state(
    session: Session,
    *,
    tenant_id: str,
    subject: EntityReference,
    namespace: str,
    key: str,
    value: dict[str, Any],
    source: str,
    expected_version: int | None = None,
    effective_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> EntityStateRow:
    if not _entity_exists(session, tenant_id, subject):
        raise TenantScopeError("STATE_SUBJECT_NOT_IN_TENANT")
    query = select(EntityStateRow).where(
            EntityStateRow.tenant_id == tenant_id,
            EntityStateRow.subject_type == subject.entity_type.upper(),
            EntityStateRow.subject_id == subject.entity_id,
            EntityStateRow.state_namespace == namespace,
            EntityStateRow.state_key == key,
        )
    if session.bind and session.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    row = session.scalar(query)
    stamp = effective_at or now_utc()
    if row is None:
        if expected_version not in {None, 0}:
            raise ValueError("STATE_VERSION_CONFLICT")
        row = EntityStateRow(
            id=new_id(),
            tenant_id=tenant_id,
            subject_type=subject.entity_type.upper(),
            subject_id=subject.entity_id,
            state_namespace=namespace,
            state_key=key,
            state_value=value,
            source=source,
            effective_at=stamp,
            expires_at=expires_at,
            version=1,
            updated_at=now_utc(),
        )
        session.add(row)
    else:
        if expected_version is not None and row.version != expected_version:
            raise ValueError("STATE_VERSION_CONFLICT")
        row.state_value = value
        row.source = source
        row.effective_at = stamp
        row.expires_at = expires_at
        row.version += 1
        row.updated_at = now_utc()
    session.flush()
    return row
