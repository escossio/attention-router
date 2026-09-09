from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


CONTROL_PLANE_SCHEMA_VERSION = "control-plane.v0"


class CapabilityKind(StrEnum):
    DISCLOSURE = "DISCLOSURE"
    ACTION = "ACTION"
    DELEGATION = "DELEGATION"
    AUTOMATION = "AUTOMATION"


class Sensitivity(StrEnum):
    PUBLIC = "PUBLIC"
    PERSONAL = "PERSONAL"
    SENSITIVE = "SENSITIVE"
    RESTRICTED = "RESTRICTED"


class DecisionMode(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class GrantMode(StrEnum):
    NONE = "NONE"
    ONE_TIME = "ONE_TIME"
    TIME_BOUND = "TIME_BOUND"
    PERSISTENT = "PERSISTENT"


class GrantStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"


class AuthorizationState(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class _ControlPlaneModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CapabilityDefinition(_ControlPlaneModel):
    schema_version: Literal["control-plane.v0"] = CONTROL_PLANE_SCHEMA_VERSION
    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,119}$")
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    kind: CapabilityKind
    sensitivity: Sensitivity
    default_disposition: DecisionMode = DecisionMode.REQUIRE_APPROVAL
    grant_modes: list[GrantMode] = Field(default_factory=lambda: [GrantMode.NONE])
    provider_key: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_grant_modes(self) -> "CapabilityDefinition":
        modes = list(dict.fromkeys(self.grant_modes))
        if not modes:
            raise ValueError("grant_modes must contain at least one mode")
        if GrantMode.NONE in modes and len(modes) > 1:
            raise ValueError("NONE cannot be combined with grantable modes")
        self.grant_modes = modes
        return self


class ApprovalRule(_ControlPlaneModel):
    schema_version: Literal["control-plane.v0"] = CONTROL_PLANE_SCHEMA_VERSION
    id: str = Field(min_length=1, max_length=64)
    capability_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,119}$")
    priority: int = Field(default=100, ge=0, le=100000)
    requester_selector: dict[str, Any] = Field(default_factory=dict)
    context_selector: dict[str, Any] = Field(default_factory=dict)
    resolution: DecisionMode = DecisionMode.REQUIRE_APPROVAL
    grant_on_approval: GrantMode = GrantMode.NONE
    grant_ttl_seconds: int | None = Field(default=None, ge=60, le=31536000)
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_grant(self) -> "ApprovalRule":
        if self.resolution != DecisionMode.REQUIRE_APPROVAL:
            if self.grant_on_approval != GrantMode.NONE or self.grant_ttl_seconds is not None:
                raise ValueError("grants are only created from explicit approval")
            return self
        if self.grant_on_approval == GrantMode.TIME_BOUND and self.grant_ttl_seconds is None:
            raise ValueError("TIME_BOUND grants require grant_ttl_seconds")
        if self.grant_on_approval != GrantMode.TIME_BOUND and self.grant_ttl_seconds is not None:
            raise ValueError("grant_ttl_seconds is only valid for TIME_BOUND grants")
        return self


class PermissionGrant(_ControlPlaneModel):
    schema_version: Literal["control-plane.v0"] = CONTROL_PLANE_SCHEMA_VERSION
    id: str = Field(min_length=1, max_length=64)
    capability_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,119}$")
    subject_actor_key: str = Field(min_length=1, max_length=120)
    grantee_actor_key: str = Field(min_length=1, max_length=120)
    mode: GrantMode
    status: GrantStatus = GrantStatus.ACTIVE
    valid_from: datetime
    expires_at: datetime | None = None
    source_authorization_id: str = Field(min_length=1, max_length=64)
    constraints: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_validity(self) -> "PermissionGrant":
        if self.mode == GrantMode.NONE:
            raise ValueError("a persisted permission cannot use grant mode NONE")
        if self.mode == GrantMode.TIME_BOUND and self.expires_at is None:
            raise ValueError("TIME_BOUND grants require expires_at")
        if self.expires_at is not None and self.expires_at <= self.valid_from:
            raise ValueError("expires_at must be after valid_from")
        return self


class AuthorizationRequest(_ControlPlaneModel):
    schema_version: Literal["control-plane.v0"] = CONTROL_PLANE_SCHEMA_VERSION
    id: str = Field(min_length=1, max_length=64)
    capability_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,119}$")
    subject_actor_key: str = Field(min_length=1, max_length=120)
    requester_actor_key: str = Field(min_length=1, max_length=120)
    state: AuthorizationState = AuthorizationState.PENDING
    requested_at: datetime
    expires_at: datetime
    request_context: dict[str, Any] = Field(default_factory=dict)
    matched_rule_id: str | None = Field(default=None, max_length=64)
    resulting_grant_id: str | None = Field(default=None, max_length=64)
    correlation_id: str = Field(min_length=1, max_length=64)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_window(self) -> "AuthorizationRequest":
        if self.expires_at <= self.requested_at:
            raise ValueError("expires_at must be after requested_at")
        if self.resulting_grant_id is not None and self.state != AuthorizationState.APPROVED:
            raise ValueError("only approved requests can reference a resulting grant")
        return self


def contract_schema() -> dict[str, dict[str, Any]]:
    """Return transport-neutral JSON Schemas for UI, API and future mobile clients."""

    return {
        "capability": CapabilityDefinition.model_json_schema(),
        "approval_rule": ApprovalRule.model_json_schema(),
        "permission_grant": PermissionGrant.model_json_schema(),
        "authorization_request": AuthorizationRequest.model_json_schema(),
    }
