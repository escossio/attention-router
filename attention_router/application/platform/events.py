from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.core.events import (
    EventEnvelope,
    EventOrigin,
    OperatorAuthority,
    authorize_origin,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID, TenantScopeError
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    CanonicalEventRow,
    InboundEventRow,
    RelationshipRow,
    ResourceRow,
    TenantRow,
    TimelineEventRow,
)
from attention_router.observability.tracing import safe_set_attribute, set_outcome, start_span


def _occurred_at(event: InboundEventRow) -> datetime:
    raw = (event.payload or {}).get("occurred_at")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return event.received_at


def event_to_envelope(row: CanonicalEventRow) -> EventEnvelope:
    return EventEnvelope(
        event_id=row.id,
        tenant_id=row.tenant_id,
        origin=EventOrigin(row.origin),
        event_type=row.event_type,
        actor_id=row.actor_id,
        resource_id=row.resource_id,
        channel=row.channel,
        payload_type=row.payload_type,
        payload_ref=row.payload_ref,
        occurred_at=row.occurred_at,
        received_at=row.received_at,
        correlation_id=row.correlation_id,
        causation_id=row.causation_id,
        metadata_sanitized=row.metadata_sanitized,
    )


def record_timeline_event(
    session: Session,
    *,
    tenant_id: str,
    event_type: str,
    occurred_at: datetime,
    provenance: str,
    canonical_event_id: str | None = None,
    actor_id: str | None = None,
    relationship_id: str | None = None,
    resource_id: str | None = None,
    event_ref: dict[str, Any] | None = None,
    visibility: str = "PRIVATE",
    metadata: dict[str, Any] | None = None,
) -> TimelineEventRow:
    if session.get(TenantRow, tenant_id) is None:
        raise TenantScopeError("TENANT_NOT_FOUND:timeline")
    for model, row_id, label in (
        (CanonicalEventRow, canonical_event_id, "canonical_event"),
        (RelationshipRow, relationship_id, "relationship"),
        (ResourceRow, resource_id, "resource"),
    ):
        if not row_id:
            continue
        referenced = session.get(model, row_id)
        if referenced is None or referenced.tenant_id != tenant_id:
            raise TenantScopeError(f"TENANT_SCOPE_MISMATCH:timeline_{label}")
    if canonical_event_id:
        existing = session.scalar(
            select(TimelineEventRow).where(
                TimelineEventRow.tenant_id == tenant_id,
                TimelineEventRow.canonical_event_id == canonical_event_id,
                TimelineEventRow.event_type == event_type,
            )
        )
        if existing:
            return existing
    row = TimelineEventRow(
        id=new_id(),
        tenant_id=tenant_id,
        canonical_event_id=canonical_event_id,
        actor_id=actor_id,
        relationship_id=relationship_id,
        resource_id=resource_id,
        event_type=event_type,
        event_ref=event_ref or {},
        occurred_at=occurred_at,
        visibility=visibility,
        provenance=provenance,
        metadata_json=metadata or {},
    )
    session.add(row)
    session.flush()
    return row


def normalize_inbound_event(
    session: Session,
    event: InboundEventRow,
    *,
    actor_id: str | None = None,
) -> CanonicalEventRow:
    existing = session.scalar(
        select(CanonicalEventRow).where(CanonicalEventRow.inbound_event_id == event.id)
    )
    if existing:
        if existing.tenant_id != event.tenant_id:
            raise TenantScopeError("TENANT_SCOPE_MISMATCH:canonical_event")
        if (
            existing.origin == EventOrigin.EXTERNAL_INBOUND.value
            and (event.payload or {}).get("event_origin") == EventOrigin.OWNER_COMMAND.value
            and (event.payload or {}).get("owner_authenticated") is True
        ):
            existing.origin = EventOrigin.OWNER_COMMAND.value
            existing.metadata_sanitized = {
                **existing.metadata_sanitized,
                "owner_authenticated": True,
            }
            session.flush()
        return existing
    with start_span("event.normalize") as span:
        tenant_id = event.tenant_id or DEFAULT_TENANT_ID
        channel = (event.payload or {}).get("channel") or event.source
        row = CanonicalEventRow(
            id=new_id(),
            tenant_id=tenant_id,
            origin=(
                EventOrigin.OWNER_COMMAND.value
                if (event.payload or {}).get("event_origin") == EventOrigin.OWNER_COMMAND.value
                and (event.payload or {}).get("owner_authenticated") is True
                else EventOrigin.EXTERNAL_INBOUND.value
            ),
            event_type=event.event_type,
            actor_id=actor_id or (event.payload or {}).get("actor_id"),
            resource_id=None,
            channel=str(channel)[:80],
            payload_type="INBOUND_EVENT_REFERENCE",
            payload_ref={"inbound_event_id": event.id},
            occurred_at=_occurred_at(event),
            received_at=event.received_at,
            correlation_id=event.correlation_id,
            causation_id=None,
            inbound_event_id=event.id,
            metadata_sanitized={
                "source": event.source,
                "from_me": bool(((event.payload or {}).get("metadata") or {}).get("from_me")),
                "owner_authenticated": (event.payload or {}).get("owner_authenticated") is True,
                "scenario_id": (event.payload or {}).get("scenario_id"),
                "stimulus_id": (event.payload or {}).get("stimulus_id"),
            },
            lineage_classification=event.lineage_classification,
            scenario_run_id=event.scenario_run_id,
            scenario_step_run_id=event.scenario_step_run_id,
        )
        session.add(row)
        session.flush()
        record_timeline_event(
            session,
            tenant_id=tenant_id,
            canonical_event_id=row.id,
            actor_id=row.actor_id,
            event_type="MESSAGE_RECEIVED" if event.event_type == "message" else "EVENT_RECEIVED",
            occurred_at=row.occurred_at,
            provenance="canonical_event_bridge",
            event_ref={"canonical_event_id": row.id},
        )
        safe_set_attribute(span, "attention.tenant_id", tenant_id)
        safe_set_attribute(span, "attention.event_origin", row.origin)
        safe_set_attribute(span, "attention.channel", row.channel)
        set_outcome(span, "NORMALIZED")
        return row


def create_canonical_event(
    session: Session,
    *,
    tenant_id: str,
    requested_origin: EventOrigin,
    event_type: str,
    payload_type: str,
    correlation_id: str,
    actor_id: str | None = None,
    resource_id: str | None = None,
    channel: str | None = None,
    payload_ref: dict[str, Any] | None = None,
    causation_id: str | None = None,
    occurred_at: datetime | None = None,
    metadata_sanitized: dict[str, Any] | None = None,
    operator_authority: OperatorAuthority | None = None,
    lineage_classification: str = "HISTORICAL_UNKNOWN",
    scenario_run_id: str | None = None,
    scenario_step_run_id: str | None = None,
) -> CanonicalEventRow:
    if session.get(TenantRow, tenant_id) is None:
        raise TenantScopeError("TENANT_NOT_FOUND:event")
    if resource_id:
        resource = session.get(ResourceRow, resource_id)
        if resource is None or resource.tenant_id != tenant_id:
            raise TenantScopeError("TENANT_SCOPE_MISMATCH:event_resource")
    origin = authorize_origin(requested_origin, operator_authority)
    if operator_authority and operator_authority.tenant_id != tenant_id:
        raise TenantScopeError("TENANT_SCOPE_MISMATCH:owner_command")
    stamp = occurred_at or now_utc()
    with start_span("event.normalize") as span:
        row = CanonicalEventRow(
            id=new_id(),
            tenant_id=tenant_id,
            origin=origin.value,
            event_type=event_type,
            actor_id=actor_id,
            resource_id=resource_id,
            channel=channel,
            payload_type=payload_type,
            payload_ref=payload_ref or {},
            occurred_at=stamp,
            received_at=now_utc(),
            correlation_id=correlation_id,
            causation_id=causation_id,
            inbound_event_id=None,
            metadata_sanitized=metadata_sanitized or {},
            lineage_classification=lineage_classification,
            scenario_run_id=scenario_run_id,
            scenario_step_run_id=scenario_step_run_id,
        )
        session.add(row)
        session.flush()
        safe_set_attribute(span, "attention.tenant_id", tenant_id)
        safe_set_attribute(span, "attention.event_origin", origin.value)
        set_outcome(span, "NORMALIZED")
        return row
