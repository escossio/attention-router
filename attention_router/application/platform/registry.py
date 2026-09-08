from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session

from attention_router.core.authority import AuthorityResult, evaluate_effective_authority
from attention_router.core.capabilities import (
    CapabilityAvailability,
    CapabilityRequest,
    CapabilityResolution,
    CapabilityResolutionStatus,
)
from attention_router.core.providers import ProviderHealth, ProviderState
from attention_router.core.tenancy import DEFAULT_TENANT_ID, DEFAULT_TENANT_SLUG, TenantScopeError
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    CapabilityDefinitionRow,
    CapabilityGrantRow,
    CapabilityVersionRow,
    ProviderBindingRow,
    ProviderDefinitionRow,
    ProviderInstanceRow,
    ResourceRow,
    TenantRow,
)
from attention_router.observability.tracing import safe_set_attribute, set_outcome, start_span
from attention_router.provisioning.manifests import (
    CapabilityManifest,
    ProviderManifest,
    load_capability_manifest,
    load_provider_manifest,
)


@dataclass(frozen=True)
class ProviderResolution:
    binding: ProviderBindingRow | None
    instance: ProviderInstanceRow | None
    definition: ProviderDefinitionRow | None
    reason_code: str


def ensure_default_tenant(session: Session) -> TenantRow:
    row = session.get(TenantRow, DEFAULT_TENANT_ID)
    if row is None:
        stamp = now_utc()
        row = TenantRow(
            id=DEFAULT_TENANT_ID,
            slug=DEFAULT_TENANT_SLUG,
            name="Alex",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
        session.add(row)
        session.flush()
    return row


def sync_capability_definitions(
    session: Session,
    manifest: CapabilityManifest | None = None,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> dict[str, int]:
    manifest = manifest or load_capability_manifest()
    if session.get(TenantRow, tenant_id) is None:
        raise TenantScopeError("TENANT_NOT_FOUND:capability_sync")
    created = updated = unchanged = 0
    for item in manifest.capabilities:
        row = session.scalar(
            select(CapabilityDefinitionRow).where(
                CapabilityDefinitionRow.tenant_id == tenant_id,
                CapabilityDefinitionRow.canonical_name == item.canonical_name,
            )
        )
        if row is None:
            row = CapabilityDefinitionRow(
                id=new_id(),
                tenant_id=tenant_id,
                canonical_name=item.canonical_name,
                domain=item.domain,
                description=item.description,
                availability_state=item.availability_state.value,
                current_version_id=None,
                created_at=now_utc(),
                updated_at=now_utc(),
            )
            session.add(row)
            session.flush()
            created += 1
        version = session.scalar(
            select(CapabilityVersionRow).where(
                CapabilityVersionRow.capability_id == row.id,
                CapabilityVersionRow.manifest_checksum == item.checksum,
            )
        )
        if version is None:
            occupied = session.scalar(
                select(CapabilityVersionRow).where(
                    CapabilityVersionRow.capability_id == row.id,
                    CapabilityVersionRow.version == item.version,
                )
            )
            if occupied is not None:
                raise ValueError(f"CAPABILITY_VERSION_IMMUTABLE:{item.canonical_name}:{item.version}")
            version = CapabilityVersionRow(
                id=new_id(),
                capability_id=row.id,
                version=item.version,
                operation_type=item.operation_type.value,
                input_schema=item.input_schema,
                output_schema=item.output_schema,
                required_permissions=item.required_permissions,
                required_provider_interface=item.required_provider_interface,
                sensitivity=item.sensitivity,
                side_effect=item.side_effect,
                default_approval_policy=item.default_approval_policy,
                availability_state=item.availability_state.value,
                manifest_checksum=item.checksum,
                metadata_json=item.metadata,
                created_at=now_utc(),
            )
            session.add(version)
            session.flush()
            updated += 1
        else:
            unchanged += 1
        row.domain = item.domain
        row.description = item.description
        row.availability_state = item.availability_state.value
        row.current_version_id = version.id
        row.updated_at = now_utc()
    return {"created": created, "updated": updated, "unchanged": unchanged}


def sync_provider_definitions(
    session: Session,
    manifest: ProviderManifest | None = None,
) -> dict[str, int]:
    manifest = manifest or load_provider_manifest()
    created = updated = 0
    for item in manifest.providers:
        row = session.scalar(
            select(ProviderDefinitionRow).where(
                ProviderDefinitionRow.canonical_name == item.canonical_name
            )
        )
        if row is None:
            row = ProviderDefinitionRow(
                id=new_id(),
                canonical_name=item.canonical_name,
                interface_name=item.interface_name,
                description=item.description,
                contract_version=item.contract_version,
                created_at=now_utc(),
            )
            session.add(row)
            created += 1
        else:
            if row.interface_name != item.interface_name:
                raise ValueError(f"PROVIDER_INTERFACE_IMMUTABLE:{item.canonical_name}")
            row.description = item.description
            row.contract_version = item.contract_version
            updated += 1
    return {"created": created, "updated": updated}


def sync_platform_registry(
    session: Session,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> dict[str, Any]:
    ensure_default_tenant(session)
    return {
        "tenant_id": tenant_id,
        "capabilities": sync_capability_definitions(session, tenant_id=tenant_id),
        "providers": sync_provider_definitions(session),
    }


def capability_and_version(
    session: Session,
    tenant_id: str,
    canonical_name: str,
) -> tuple[CapabilityDefinitionRow | None, CapabilityVersionRow | None]:
    definition = session.scalar(
        select(CapabilityDefinitionRow).where(
            CapabilityDefinitionRow.tenant_id == tenant_id,
            CapabilityDefinitionRow.canonical_name == canonical_name,
        )
    )
    version = (
        session.get(CapabilityVersionRow, definition.current_version_id)
        if definition and definition.current_version_id
        else None
    )
    return definition, version


def resolve_provider(
    session: Session,
    *,
    tenant_id: str,
    capability: CapabilityDefinitionRow,
    version: CapabilityVersionRow,
    resource_id: str | None = None,
) -> ProviderResolution:
    with start_span("provider.resolve") as span:
        safe_set_attribute(span, "attention.tenant_id", tenant_id)
        safe_set_attribute(span, "attention.capability", capability.canonical_name)
        safe_set_attribute(span, "attention.provider_interface", version.required_provider_interface)
        if not version.required_provider_interface:
            set_outcome(span, "NOT_REQUIRED")
            return ProviderResolution(None, None, None, "PROVIDER_NOT_REQUIRED")
        binding = session.scalar(
            select(ProviderBindingRow)
            .where(
                ProviderBindingRow.tenant_id == tenant_id,
                ProviderBindingRow.capability_id == capability.id,
                ProviderBindingRow.status == "ACTIVE",
                or_(ProviderBindingRow.resource_id == resource_id, ProviderBindingRow.resource_id.is_(None)),
            )
            .order_by(
                case((ProviderBindingRow.resource_id == resource_id, 1), else_=0).desc(),
                ProviderBindingRow.priority.desc(),
                ProviderBindingRow.created_at,
            )
        )
        if binding is None:
            set_outcome(span, "PROVIDER_MISSING")
            return ProviderResolution(None, None, None, "PROVIDER_MISSING")
        instance = session.get(ProviderInstanceRow, binding.provider_instance_id)
        if instance is None or instance.tenant_id != tenant_id:
            set_outcome(span, "TENANT_PROVIDER_MISMATCH", error=True)
            return ProviderResolution(binding, None, None, "TENANT_PROVIDER_MISMATCH")
        definition = session.get(ProviderDefinitionRow, instance.provider_definition_id)
        if definition is None or definition.interface_name != version.required_provider_interface:
            set_outcome(span, "PROVIDER_INTERFACE_MISMATCH", error=True)
            return ProviderResolution(binding, instance, definition, "PROVIDER_INTERFACE_MISMATCH")
        if instance.state in {
            ProviderState.SUSPENDED.value,
            ProviderState.UNAVAILABLE.value,
            ProviderState.UNCONFIGURED.value,
        } or instance.health != ProviderHealth.HEALTHY.value:
            set_outcome(span, "PROVIDER_UNAVAILABLE")
            return ProviderResolution(binding, instance, definition, "PROVIDER_UNAVAILABLE")
        safe_set_attribute(span, "attention.provider_health", instance.health)
        set_outcome(span, "RESOLVED")
        return ProviderResolution(binding, instance, definition, "PROVIDER_RESOLVED")


def active_grant_exists(
    session: Session,
    *,
    tenant_id: str,
    capability_id: str,
    grantee_type: str,
    grantee_id: str,
    resource_id: str | None = None,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now(timezone.utc)
    # Select only columns present in the live capability-grants contract.
    # Some older live schemas do not yet carry optional revocation metadata.
    row = session.scalar(
        select(CapabilityGrantRow.id).where(
            CapabilityGrantRow.tenant_id == tenant_id,
            CapabilityGrantRow.capability_id == capability_id,
            CapabilityGrantRow.grantee_type == grantee_type,
            CapabilityGrantRow.grantee_id == grantee_id,
            CapabilityGrantRow.status == "ACTIVE",
            CapabilityGrantRow.valid_from <= now,
            or_(CapabilityGrantRow.valid_until.is_(None), CapabilityGrantRow.valid_until > now),
            or_(
                CapabilityGrantRow.target_resource_id.is_(None),
                CapabilityGrantRow.target_resource_id == resource_id,
            ),
        )
    )
    return row is not None


def resolve_capability_request(
    session: Session,
    request: CapabilityRequest,
    *,
    tenant_id: str,
    grantee_type: str,
    grantee_id: str,
    policy_allows: bool,
    resource_id: str | None = None,
    owner_authorized: bool = False,
) -> CapabilityResolution:
    with start_span("capability.resolve") as span:
        safe_set_attribute(span, "attention.tenant_id", tenant_id)
        safe_set_attribute(span, "attention.capability", request.capability)
        definition, version = capability_and_version(session, tenant_id, request.capability)
        if definition is None or version is None:
            set_outcome(span, "UNKNOWN")
            return CapabilityResolution(
                capability=request.capability,
                status=CapabilityResolutionStatus.UNKNOWN,
                reason_code="UNKNOWN_CAPABILITY",
                authority_result="UNAVAILABLE",
            )
        availability = CapabilityAvailability(definition.availability_state)
        provider = resolve_provider(
            session,
            tenant_id=tenant_id,
            capability=definition,
            version=version,
            resource_id=resource_id,
        )
        provider_unavailable = bool(
            version.required_provider_interface
            and provider.reason_code != "PROVIDER_RESOLVED"
        )
        if provider_unavailable or availability not in {
            CapabilityAvailability.PROVISIONED,
            CapabilityAvailability.SANDBOX_PROVED,
            CapabilityAvailability.OPERATIONAL,
        }:
            set_outcome(span, "KNOWN_BUT_UNAVAILABLE")
            return CapabilityResolution(
                capability=request.capability,
                status=CapabilityResolutionStatus.KNOWN_BUT_UNAVAILABLE,
                reason_code="CAPABILITY_UNAVAILABLE",
                provider_interface=version.required_provider_interface,
                authority_result="UNAVAILABLE",
            )
        grant = active_grant_exists(
            session,
            tenant_id=tenant_id,
            capability_id=definition.id,
            grantee_type=grantee_type,
            grantee_id=grantee_id,
            resource_id=resource_id,
        )
        # Authenticated owner authority is an ingress property, never text supplied by Andy.
        effective_grant = grant or owner_authorized
        with start_span("authority.evaluate") as authority_span:
            authority = evaluate_effective_authority(
                availability=availability,
                policy_allows=policy_allows,
                grant_active=effective_grant,
                side_effect=version.side_effect,
                default_approval_policy=version.default_approval_policy,
            )
            safe_set_attribute(authority_span, "attention.tenant_id", tenant_id)
            safe_set_attribute(authority_span, "attention.capability", request.capability)
            safe_set_attribute(authority_span, "attention.authority_result", authority.result.value)
            safe_set_attribute(authority_span, "attention.reason_code", authority.reason_code)
            set_outcome(authority_span, authority.result.value)
        if authority.result == AuthorityResult.DENY:
            status = CapabilityResolutionStatus.AVAILABLE_NOT_AUTHORIZED
        elif authority.result == AuthorityResult.REQUIRES_APPROVAL:
            status = CapabilityResolutionStatus.REQUIRES_APPROVAL
        elif availability == CapabilityAvailability.OPERATIONAL:
            status = CapabilityResolutionStatus.OPERATIONAL
        else:
            status = CapabilityResolutionStatus.AUTHORIZED
        set_outcome(span, status.value)
        return CapabilityResolution(
            capability=request.capability,
            status=status,
            reason_code=authority.reason_code,
            provider_instance_id=provider.instance.id if provider.instance else None,
            provider_interface=version.required_provider_interface,
            authority_result=authority.result.value,
            execution_allowed=authority.result == AuthorityResult.ALLOW,
            approval_required=authority.result == AuthorityResult.REQUIRES_APPROVAL,
        )


def matrix_status(session: Session, *, tenant_id: str = DEFAULT_TENANT_ID) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(CapabilityDefinitionRow)
        .where(CapabilityDefinitionRow.tenant_id == tenant_id)
        .order_by(CapabilityDefinitionRow.canonical_name)
    ).all()
    output: list[dict[str, Any]] = []
    for row in rows:
        version = session.get(CapabilityVersionRow, row.current_version_id) if row.current_version_id else None
        provider = resolve_provider(
            session,
            tenant_id=tenant_id,
            capability=row,
            version=version,
        ) if version else ProviderResolution(None, None, None, "VERSION_MISSING")
        output.append({
            "capability": row.canonical_name,
            "version": version.version if version else None,
            "state": row.availability_state,
            "required_provider": version.required_provider_interface if version else None,
            "bound_provider": provider.instance.canonical_name if provider.instance else None,
            "health": provider.instance.health if provider.instance else None,
            "side_effect": version.side_effect if version else None,
            "sensitivity": version.sensitivity if version else None,
        })
    return output


def register_provider_instance(
    session: Session,
    *,
    tenant_id: str,
    provider_definition_name: str,
    canonical_name: str,
    state: str = "CONFIGURED",
    health: str = "HEALTHY",
    config_reference: dict[str, Any] | None = None,
) -> ProviderInstanceRow:
    state = ProviderState(state).value
    health = ProviderHealth(health).value
    definition = session.scalar(
        select(ProviderDefinitionRow).where(
            ProviderDefinitionRow.canonical_name == provider_definition_name
        )
    )
    if definition is None or session.get(TenantRow, tenant_id) is None:
        raise KeyError("provider definition or tenant not found")
    row = ProviderInstanceRow(
        id=new_id(),
        tenant_id=tenant_id,
        provider_definition_id=definition.id,
        canonical_name=canonical_name,
        state=state,
        health=health,
        config_reference=config_reference or {},
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row


def bind_provider(
    session: Session,
    *,
    tenant_id: str,
    capability_name: str,
    provider_instance_id: str,
    resource_id: str | None = None,
    priority: int = 0,
) -> ProviderBindingRow:
    capability, version = capability_and_version(session, tenant_id, capability_name)
    instance = session.get(ProviderInstanceRow, provider_instance_id)
    if capability is None or version is None or instance is None:
        raise KeyError("capability or provider instance not found")
    if instance.tenant_id != tenant_id:
        raise TenantScopeError("TENANT_SCOPE_MISMATCH:provider_instance")
    if resource_id:
        resource = session.get(ResourceRow, resource_id)
        if resource is None or resource.tenant_id != tenant_id:
            raise TenantScopeError("TENANT_SCOPE_MISMATCH:provider_resource")
    definition = session.get(ProviderDefinitionRow, instance.provider_definition_id)
    if (
        definition is None
        or not version.required_provider_interface
        or definition.interface_name != version.required_provider_interface
    ):
        raise ValueError("PROVIDER_INTERFACE_MISMATCH")
    row = ProviderBindingRow(
        id=new_id(),
        tenant_id=tenant_id,
        capability_id=capability.id,
        resource_id=resource_id,
        provider_instance_id=provider_instance_id,
        status="ACTIVE",
        priority=priority,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row
