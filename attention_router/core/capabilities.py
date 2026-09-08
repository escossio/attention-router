from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


CAPABILITY_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


class OperationType(StrEnum):
    READ = "READ"
    WRITE = "WRITE"
    ACTION = "ACTION"


class CapabilityAvailability(StrEnum):
    DECLARED = "DECLARED"
    CONTRACT_DEFINED = "CONTRACT_DEFINED"
    PROVIDER_MISSING = "PROVIDER_MISSING"
    PROVISIONED = "PROVISIONED"
    SANDBOX_PROVED = "SANDBOX_PROVED"
    OPERATIONAL = "OPERATIONAL"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"


class CapabilityRequest(BaseModel):
    """Semantic request from Andy; it never grants execution authority."""

    model_config = ConfigDict(extra="forbid")

    capability: str
    target: dict[str, Any] | None = None
    resource: dict[str, Any] | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    user_requested: bool = False
    confidence: Literal["high", "medium", "low"] = "medium"

    @field_validator("capability")
    @classmethod
    def valid_capability_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not CAPABILITY_NAME_RE.fullmatch(normalized):
            raise ValueError("capability must use a dotted canonical name")
        return normalized


class CapabilityResolutionStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    KNOWN_BUT_UNAVAILABLE = "KNOWN_BUT_UNAVAILABLE"
    AVAILABLE_NOT_AUTHORIZED = "AVAILABLE_NOT_AUTHORIZED"
    AUTHORIZED = "AUTHORIZED"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
    OPERATIONAL = "OPERATIONAL"


class CapabilityResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str
    status: CapabilityResolutionStatus
    reason_code: str
    provider_instance_id: str | None = None
    provider_interface: str | None = None
    authority_result: str
    execution_allowed: bool = False
    approval_required: bool = False
