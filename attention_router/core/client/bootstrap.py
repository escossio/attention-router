"""Infrastructure-independent V0.3B device-bootstrap authority vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class TenantRole(StrEnum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"


class MembershipStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"


class ClientDeviceStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class ClientDevicePlatform(StrEnum):
    ANDROID = "ANDROID"


class ClientDeviceRole(StrEnum):
    CLIENT = "CLIENT"
    CAPABILITY_NODE = "CAPABILITY_NODE"


@dataclass(frozen=True, slots=True)
class TenantMembershipView:
    membership_id: str
    tenant_id: str
    role: TenantRole
    status: MembershipStatus


@dataclass(frozen=True, slots=True)
class ClientDeviceView:
    device_id: str
    public_key_fingerprint: str
    canonical_name: str
    platform: ClientDevicePlatform
    roles: tuple[ClientDeviceRole, ...]
    status: ClientDeviceStatus


@dataclass(frozen=True, slots=True)
class DeviceBootstrapChallenge:
    bootstrap_challenge_id: str
    challenge_b64url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class DeviceBootstrapEstablished:
    human_identity_id: str
    memberships: tuple[TenantMembershipView, ...]
    initial_tenant_id: str | None
    device: ClientDeviceView
    status: str = "DEVICE_BOOTSTRAP_ESTABLISHED"
