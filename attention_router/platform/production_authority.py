"""Fail-closed validation for production execution authority bundles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
import hashlib
import json
import re


class ProductionAuthorityDenied(ValueError):
    """Raised when a production authority graph is incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class FrozenAuthority:
    target_count: int
    outbound_messages: int
    action_count: int
    retries: int
    transport: str
    operation: str
    capability: str
    expires_at: datetime
    target_identity: tuple[str, ...] = ()


SEMANTIC_SCOPE_KEYS = frozenset({
    "scenario",
    "execution_class",
    "safety_set",
    "policies",
    "authority_profile",
    "frozen_authority",
    "target",
    "audience",
    "immutable_inputs",
})
FROZEN_AUTHORITY_KEYS = frozenset({
    "target_count",
    "outbound_messages",
    "action_count",
    "retries",
    "transport",
    "operation",
    "capability",
    "expires_at",
    "target_identity",
})


def _environment(obj: Any) -> str:
    value = getattr(obj, "environment_classification", None)
    if value != "PRODUCTION":
        raise ProductionAuthorityDenied("PRODUCTION_DEPENDENCY_REQUIRED")
    if getattr(obj, "status", "ACTIVE") != "ACTIVE":
        raise ProductionAuthorityDenied("PRODUCTION_DEPENDENCY_INACTIVE")
    if getattr(obj, "enabled", True) is not True:
        raise ProductionAuthorityDenied("PRODUCTION_DEPENDENCY_INACTIVE")
    return value


def validate_production_scope_dependency_graph(*, scenario_version: Any, execution_class: Any,
                                                safety_set: Any, policy: Any, authority_profile: Any,
                                                transport: str, operation: str, capability: str,
                                                frozen_authority: FrozenAuthority | None = None,
                                                targets: list[Any] | tuple[Any, ...] | None = None,
                                                audience: Any = None,
                                                expires_at: datetime | None = None) -> None:
    """Validate the complete graph before a production intent is persisted."""
    _environment(scenario_version)
    _environment(execution_class)
    _environment(safety_set)
    _environment(policy)
    _environment(authority_profile)
    if transport not in (getattr(execution_class, "allowed_transports", None) or []):
        raise ProductionAuthorityDenied("TRANSPORT_NOT_ALLOWED")
    if operation not in (getattr(execution_class, "allowed_operations", None) or []):
        raise ProductionAuthorityDenied("OPERATION_NOT_ALLOWED")
    if capability not in (getattr(execution_class, "allowed_capabilities", None) or []):
        raise ProductionAuthorityDenied("CAPABILITY_NOT_ALLOWED")
    for obj, field, expected in (
        (safety_set, "allowed_transport", transport),
        (safety_set, "allowed_operation", operation),
        (safety_set, "allowed_capability", capability),
    ):
        if getattr(obj, field) != expected:
            raise ProductionAuthorityDenied(f"SAFETY_{field.upper()}_MISMATCH")
    if getattr(safety_set, "execution_class_version_id", None) not in (None, getattr(execution_class, "id", None)):
        raise ProductionAuthorityDenied("SAFETY_EXECUTION_CLASS_MISMATCH")
    if frozen_authority is not None:
        _validate_authority_ceiling(frozen_authority, authority_profile, safety_set)
    if expires_at is None and frozen_authority is not None:
        expires_at = frozen_authority.expires_at
    if expires_at is None:
        raise ProductionAuthorityDenied("EXPIRY_REQUIRED")
    validate_not_expired(expires_at)
    if targets is not None:
        if len(targets) != 1:
            raise ProductionAuthorityDenied("EXACTLY_ONE_RECIPIENT_REQUIRED")
        target = targets[0]
        validate_single_recipient(
            target_type=getattr(target, "target_type", None), target_count=1,
            endpoint_transport=getattr(getattr(target, "recipient_endpoint", target), "transport", None),
            transport=transport,
        )
    if audience is not None and getattr(audience, "audience_type", audience.get("audience_type") if isinstance(audience, dict) else None) != "single_represented_owner_contact":
        raise ProductionAuthorityDenied("AUDIENCE_NOT_ALLOWED")


def validate_single_recipient(*, target_type: str, target_count: int, endpoint_transport: str,
                              transport: str) -> None:
    if target_type != "WHATSAPP_RECIPIENT_ENDPOINT":
        raise ProductionAuthorityDenied("TARGET_TYPE_NOT_ALLOWED")
    if target_count != 1:
        raise ProductionAuthorityDenied("EXACTLY_ONE_RECIPIENT_REQUIRED")
    if endpoint_transport != transport:
        raise ProductionAuthorityDenied("RECIPIENT_TRANSPORT_MISMATCH")


def authority_is_subset(child: FrozenAuthority, parent: FrozenAuthority) -> bool:
    child_expiry = _utc(child.expires_at)
    parent_expiry = _utc(parent.expires_at)
    return (
        child.target_count <= parent.target_count
        and child.outbound_messages <= parent.outbound_messages
        and child.action_count <= parent.action_count
        and child.retries <= parent.retries
        and child.transport == parent.transport
        and child.operation == parent.operation
        and child.capability == parent.capability
        and child_expiry <= parent_expiry
        and set(child.target_identity).issubset(parent.target_identity)
    )


def _validate_authority_ceiling(authority: FrozenAuthority, profile: Any, safety: Any) -> None:
    for field in ("target_count", "outbound_messages", "action_count", "retries"):
        value = getattr(authority, field)
        if value < 0 or value > getattr(profile, {"target_count": "max_target_cardinality", "outbound_messages": "max_outbound_messages", "action_count": "max_action_count", "retries": "max_retries"}[field]):
            raise ProductionAuthorityDenied("AUTHORITY_EXCEEDS_PROFILE_CEILING")
    if authority.target_count > getattr(safety, "max_target_cardinality", authority.target_count) or authority.outbound_messages > getattr(safety, "max_outbound_messages", authority.outbound_messages) or authority.action_count > getattr(safety, "max_action_count", authority.action_count):
        raise ProductionAuthorityDenied("AUTHORITY_EXCEEDS_SAFETY_CEILING")
    if authority.transport != getattr(profile, "allowed_transport", authority.transport) or authority.operation != getattr(profile, "allowed_operation", authority.operation) or authority.capability != getattr(profile, "allowed_capability", authority.capability):
        raise ProductionAuthorityDenied("AUTHORITY_SCOPE_MISMATCH")


def canonical_recipient_address(transport: str, address: str) -> str:
    """Return a deterministic semantic address; display formatting is discarded."""
    value = address.strip()
    if transport == "meta_whatsapp":
        digits = re.sub(r"\D", "", value)
        if not digits:
            raise ProductionAuthorityDenied("RECIPIENT_ADDRESS_INVALID")
        return digits
    if not value:
        raise ProductionAuthorityDenied("RECIPIENT_ADDRESS_INVALID")
    return value


def production_scope_fingerprint(*, scenario_identity: Any, execution_class: Any,
                                 safety_set: Any, policies: list[Any] | tuple[Any, ...],
                                 authority_profile: Any, frozen_authority: FrozenAuthority,
                                 tenant_identity: str, canonical_address: str,
                                 audience: Any, immutable_inputs: Any) -> str:
    material = production_scope_material(
        scenario_identity=scenario_identity,
        execution_class=execution_class,
        safety_set=safety_set,
        policies=policies,
        authority_profile=authority_profile,
        frozen_authority=frozen_authority,
        tenant_identity=tenant_identity,
        canonical_address=canonical_address,
        audience=audience,
        immutable_inputs=immutable_inputs,
    )
    return semantic_scope_fingerprint(material)


def production_scope_material(*, scenario_identity: Any, execution_class: Any,
                              safety_set: Any, policies: list[Any] | tuple[Any, ...],
                              authority_profile: Any, frozen_authority: FrozenAuthority,
                              tenant_identity: str, canonical_address: str,
                              audience: Any, immutable_inputs: Any) -> dict[str, Any]:
    """Return the complete semantic material frozen by an ExecutionIntent."""
    return {
        "scenario": scenario_identity, "execution_class": execution_class,
        "safety_set": safety_set,
        "policies": sorted(policies, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))),
        "authority_profile": authority_profile,
        "frozen_authority": {"target_count": frozen_authority.target_count, "outbound_messages": frozen_authority.outbound_messages, "action_count": frozen_authority.action_count, "retries": frozen_authority.retries, "transport": frozen_authority.transport, "operation": frozen_authority.operation, "capability": frozen_authority.capability, "expires_at": _iso(frozen_authority.expires_at), "target_identity": sorted(frozen_authority.target_identity)},
        "target": {"tenant": tenant_identity, "transport": frozen_authority.transport, "canonical_address": canonical_address},
        "audience": audience, "immutable_inputs": immutable_inputs,
    }


def semantic_scope_fingerprint(scope: dict[str, Any]) -> str:
    """Fingerprint semantic authority only; technical metadata lives outside scope."""
    return hashlib.sha256(
        json.dumps(
            scope,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()


def frozen_authority_from_scope(scope: dict[str, Any]) -> FrozenAuthority:
    """Parse the exact production scope shape without applying permissive defaults."""
    if set(scope) != SEMANTIC_SCOPE_KEYS:
        raise ProductionAuthorityDenied("SEMANTIC_SCOPE_SCHEMA_MISMATCH")
    raw = scope.get("frozen_authority")
    if not isinstance(raw, dict) or set(raw) != FROZEN_AUTHORITY_KEYS:
        raise ProductionAuthorityDenied("FROZEN_AUTHORITY_SCHEMA_MISMATCH")
    try:
        expiry = datetime.fromisoformat(str(raw["expires_at"]))
        target_identity = tuple(str(value) for value in raw["target_identity"])
        authority = FrozenAuthority(
            target_count=int(raw["target_count"]),
            outbound_messages=int(raw["outbound_messages"]),
            action_count=int(raw["action_count"]),
            retries=int(raw["retries"]),
            transport=str(raw["transport"]),
            operation=str(raw["operation"]),
            capability=str(raw["capability"]),
            expires_at=expiry,
            target_identity=target_identity,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProductionAuthorityDenied("FROZEN_AUTHORITY_INVALID") from exc
    if not isinstance(scope.get("immutable_inputs"), dict):
        raise ProductionAuthorityDenied("IMMUTABLE_INPUTS_REQUIRED")
    if not isinstance(scope.get("target"), dict):
        raise ProductionAuthorityDenied("TARGET_SCOPE_REQUIRED")
    if not isinstance(scope.get("audience"), dict):
        raise ProductionAuthorityDenied("AUDIENCE_SCOPE_REQUIRED")
    return authority


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def validate_not_expired(expires_at: datetime, now: datetime | None = None) -> None:
    timestamp = now or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    if timestamp >= _utc(expires_at):
        raise ProductionAuthorityDenied("EXECUTION_INTENT_EXPIRED")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
