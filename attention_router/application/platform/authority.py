from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from attention_router.application.platform.entities import get_resource
from attention_router.application.platform.events import record_timeline_event
from attention_router.application.platform.registry import capability_and_version
from attention_router.core.tenancy import TenantScopeError
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import CapabilityGrantRow
from attention_router.infrastructure.repository import audit


def create_capability_grant(
    session: Session,
    *,
    tenant_id: str,
    grantor_type: str,
    grantor_id: str,
    grantee_type: str,
    grantee_id: str,
    capability_name: str,
    scope: dict | None = None,
    target_resource_id: str | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    constraints: dict | None = None,
    provenance: str = "operator",
) -> CapabilityGrantRow:
    definition, _ = capability_and_version(session, tenant_id, capability_name)
    if definition is None:
        raise KeyError(capability_name)
    if target_resource_id:
        get_resource(session, tenant_id, target_resource_id)
    row = CapabilityGrantRow(
        id=new_id(),
        tenant_id=tenant_id,
        grantor_type=grantor_type,
        grantor_id=grantor_id,
        grantee_type=grantee_type,
        grantee_id=grantee_id,
        capability_id=definition.id,
        scope=scope or {},
        target_resource_id=target_resource_id,
        status="ACTIVE",
        valid_from=valid_from or now_utc(),
        valid_until=valid_until,
        constraints_json=constraints or {},
        provenance=provenance,
        created_at=now_utc(),
    )
    session.add(row)
    session.flush()
    record_timeline_event(
        session,
        tenant_id=tenant_id,
        actor_id=grantee_id if grantee_type.upper() == "ACTOR" else None,
        resource_id=target_resource_id,
        event_type="GRANT_CREATED",
        occurred_at=row.created_at,
        provenance=provenance,
        event_ref={"grant_id": row.id, "capability": capability_name},
    )
    audit(
        session,
        None,
        "capability_grant_created",
        {"grant_id": row.id, "capability": capability_name},
        tenant_id=tenant_id,
    )
    return row


def revoke_capability_grant(session: Session, *, tenant_id: str, grant_id: str) -> CapabilityGrantRow:
    row = session.get(CapabilityGrantRow, grant_id)
    if row is None:
        raise KeyError(grant_id)
    if row.tenant_id != tenant_id:
        raise TenantScopeError("TENANT_SCOPE_MISMATCH:grant")
    row.status = "REVOKED"
    row.revoked_at = now_utc()
    session.flush()
    record_timeline_event(
        session,
        tenant_id=tenant_id,
        actor_id=row.grantee_id if row.grantee_type.upper() == "ACTOR" else None,
        resource_id=row.target_resource_id,
        event_type="GRANT_REVOKED",
        occurred_at=row.revoked_at,
        provenance="grant_revocation",
        event_ref={"grant_id": row.id},
    )
    audit(
        session,
        None,
        "capability_grant_revoked",
        {"grant_id": row.id},
        tenant_id=tenant_id,
    )
    return row
