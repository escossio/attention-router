from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.platform.entities import get_resource
from attention_router.application.platform.registry import active_grant_exists, capability_and_version
from attention_router.core.devices import (
    DeviceCapabilityAnnouncement,
    DeviceHeartbeat,
    DeviceRegistrationRequest,
    DeviceRegistrationResponse,
)
from attention_router.core.tenancy import TenantScopeError
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ActorBindingRow,
    DeviceBindingRow,
    DeviceCapabilityRow,
    DeviceIdentityRow,
    DeviceRow,
    DeviceStatusRow,
)


def register_device(
    session: Session,
    *,
    tenant_id: str,
    request: DeviceRegistrationRequest,
) -> DeviceRegistrationResponse:
    identity_hash = stable_hash({
        "tenant_id": tenant_id,
        "identity_type": request.identity_type,
        "identity_reference": request.identity_reference,
    })
    existing_identity = session.scalar(
        select(DeviceIdentityRow).where(
            DeviceIdentityRow.tenant_id == tenant_id,
            DeviceIdentityRow.identity_type == request.identity_type,
            DeviceIdentityRow.identity_reference_hash == identity_hash,
        )
    )
    if existing_identity:
        device = session.get(DeviceRow, existing_identity.device_id)
        return DeviceRegistrationResponse(
            device_id=device.id,
            identity_id=existing_identity.id,
            status=device.status,
        )
    stamp = now_utc()
    device = DeviceRow(
        id=new_id(),
        tenant_id=tenant_id,
        canonical_name=request.canonical_name,
        platform=request.platform.value,
        roles=[role.value for role in request.roles],
        status="REGISTERED",
        metadata_json=request.metadata_sanitized,
        created_at=stamp,
        updated_at=stamp,
    )
    identity = DeviceIdentityRow(
        id=new_id(),
        tenant_id=tenant_id,
        device_id=device.id,
        identity_type=request.identity_type,
        identity_reference_hash=identity_hash,
        status="ACTIVE",
        created_at=stamp,
    )
    session.add_all([device, identity])
    session.flush()
    return DeviceRegistrationResponse(device_id=device.id, identity_id=identity.id, status=device.status)


def _device(session: Session, tenant_id: str, device_id: str) -> DeviceRow:
    row = session.get(DeviceRow, device_id)
    if row is None:
        raise KeyError(device_id)
    if row.tenant_id != tenant_id:
        raise TenantScopeError("TENANT_SCOPE_MISMATCH:device")
    return row


def bind_device(
    session: Session,
    *,
    tenant_id: str,
    device_id: str,
    actor_key: str | None = None,
    resource_id: str | None = None,
) -> DeviceBindingRow:
    _device(session, tenant_id, device_id)
    if actor_key:
        actor = session.scalar(
            select(ActorBindingRow).where(
                ActorBindingRow.tenant_id == tenant_id,
                ActorBindingRow.actor_key == actor_key,
            )
        )
        if actor is None:
            raise TenantScopeError("DEVICE_ACTOR_NOT_IN_TENANT")
    if resource_id:
        get_resource(session, tenant_id, resource_id)
    if not actor_key and not resource_id:
        raise ValueError("device binding requires actor or resource")
    row = DeviceBindingRow(
        id=new_id(),
        tenant_id=tenant_id,
        device_id=device_id,
        actor_key=actor_key,
        resource_id=resource_id,
        status="ACTIVE",
        created_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row


def announce_capabilities(
    session: Session,
    *,
    tenant_id: str,
    device_id: str,
    announcement: DeviceCapabilityAnnouncement,
) -> list[DeviceCapabilityRow]:
    _device(session, tenant_id, device_id)
    rows: list[DeviceCapabilityRow] = []
    for name in sorted(set(announcement.capabilities)):
        row = session.scalar(
            select(DeviceCapabilityRow).where(
                DeviceCapabilityRow.tenant_id == tenant_id,
                DeviceCapabilityRow.device_id == device_id,
                DeviceCapabilityRow.capability_name == name,
            )
        )
        if row is None:
            row = DeviceCapabilityRow(
                id=new_id(),
                tenant_id=tenant_id,
                device_id=device_id,
                capability_name=name,
                availability="ANNOUNCED",
                announced_at=announcement.announced_at,
                metadata_json={},
            )
            session.add(row)
        else:
            row.availability = "ANNOUNCED"
            row.announced_at = announcement.announced_at
        rows.append(row)
    session.flush()
    return rows


def device_capability_authorized(
    session: Session,
    *,
    tenant_id: str,
    device_id: str,
    capability_name: str,
) -> bool:
    _device(session, tenant_id, device_id)
    announced = session.scalar(
        select(DeviceCapabilityRow).where(
            DeviceCapabilityRow.tenant_id == tenant_id,
            DeviceCapabilityRow.device_id == device_id,
            DeviceCapabilityRow.capability_name == capability_name,
            DeviceCapabilityRow.availability == "ANNOUNCED",
        )
    )
    definition, _ = capability_and_version(session, tenant_id, capability_name)
    if announced is None or definition is None:
        return False
    return active_grant_exists(
        session,
        tenant_id=tenant_id,
        capability_id=definition.id,
        grantee_type="DEVICE",
        grantee_id=device_id,
    )


def record_heartbeat(
    session: Session,
    *,
    tenant_id: str,
    device_id: str,
    heartbeat: DeviceHeartbeat,
) -> DeviceStatusRow:
    device = _device(session, tenant_id, device_id)
    row = DeviceStatusRow(
        id=new_id(),
        tenant_id=tenant_id,
        device_id=device_id,
        health=heartbeat.health,
        connectivity=heartbeat.connectivity,
        observed_at=heartbeat.observed_at,
        metrics_sanitized=heartbeat.metrics_sanitized,
    )
    device.status = "ONLINE" if heartbeat.health.upper() == "HEALTHY" else "DEGRADED"
    device.updated_at = now_utc()
    session.add(row)
    session.flush()
    return row
