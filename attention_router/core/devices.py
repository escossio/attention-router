from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class DevicePlatform(StrEnum):
    ANDROID = "ANDROID"
    IOS = "IOS"
    WINDOWS = "WINDOWS"
    LINUX = "LINUX"
    EMBEDDED = "EMBEDDED"


class DeviceRole(StrEnum):
    CLIENT = "CLIENT"
    CAPABILITY_NODE = "CAPABILITY_NODE"


class DeviceRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str = Field(min_length=1, max_length=160)
    platform: DevicePlatform
    roles: list[DeviceRole] = Field(min_length=1)
    identity_type: str = Field(min_length=1, max_length=80)
    identity_reference: str = Field(min_length=1, max_length=180)
    metadata_sanitized: dict[str, Any] = Field(default_factory=dict)


class DeviceRegistrationResponse(BaseModel):
    device_id: str
    identity_id: str
    status: str


class DeviceCapabilityAnnouncement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capabilities: list[str]
    announced_at: datetime


class DeviceHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_at: datetime
    connectivity: str = Field(min_length=1, max_length=40)
    health: str = Field(min_length=1, max_length=40)
    metrics_sanitized: dict[str, Any] = Field(default_factory=dict)


class DeviceAuthenticationContext(BaseModel):
    tenant_id: str
    device_id: str
    authenticated: bool


class DeviceAuthenticator(Protocol):
    def authenticate(self, credential_reference: str) -> DeviceAuthenticationContext: ...
