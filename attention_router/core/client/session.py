"""Infrastructure-independent V0.3C client-session authority vocabulary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .bootstrap import ClientDeviceRole, ClientDeviceStatus, MembershipStatus, TenantRole


class ClientSessionState(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


@dataclass(frozen=True, slots=True)
class SessionMembershipAuthority:
    membership_id: str
    human_identity_id: str
    tenant_id: str
    role: TenantRole
    status: MembershipStatus


@dataclass(frozen=True, slots=True)
class SessionDeviceAuthority:
    device_id: str
    human_identity_id: str
    status: ClientDeviceStatus
    roles: tuple[ClientDeviceRole, ...]


@dataclass(frozen=True, slots=True)
class ClientSessionGrant:
    session_id: str
    human_identity_id: str
    device_id: str
    tenant_id: str
    state: ClientSessionState
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ClientSessionChallenge:
    session_challenge_id: str
    challenge_b64url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ClientSessionIssued:
    session_id: str
    session_token: str = field(repr=False)
    expires_at: datetime
    human_identity_id: str
    device_id: str
    tenant_id: str
    token_type: str = "Bearer"
    status: str = "CLIENT_SESSION_ESTABLISHED"


@dataclass(frozen=True, slots=True)
class ClientSessionAuthorityDecision:
    allowed: bool
    reason_code: str


class ClientSessionAuthorityError(PermissionError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def resolve_session_tenant(
    memberships: tuple[SessionMembershipAuthority, ...],
    *,
    requested_tenant_id: str | None,
) -> str:
    active = tuple(
        membership
        for membership in memberships
        if membership.status is MembershipStatus.ACTIVE
    )
    if requested_tenant_id is not None:
        matches = tuple(
            membership
            for membership in active
            if membership.tenant_id == requested_tenant_id
        )
        if len(matches) != 1:
            raise ClientSessionAuthorityError("TENANT_FORBIDDEN")
        return requested_tenant_id
    if not active:
        raise ClientSessionAuthorityError("NO_ACTIVE_MEMBERSHIP")
    if len(active) != 1:
        raise ClientSessionAuthorityError("ACTIVE_TENANT_REQUIRED")
    return active[0].tenant_id


def evaluate_client_session_authority(
    *,
    session: ClientSessionGrant,
    device: SessionDeviceAuthority,
    membership: SessionMembershipAuthority,
    tenant_active: bool,
    now: datetime,
) -> ClientSessionAuthorityDecision:
    if session.expires_at.tzinfo is None or now.tzinfo is None:
        return ClientSessionAuthorityDecision(False, "INVALID_TIME_CONTEXT")
    if session.state is not ClientSessionState.ACTIVE:
        return ClientSessionAuthorityDecision(False, "SESSION_INACTIVE")
    if session.expires_at <= now:
        return ClientSessionAuthorityDecision(False, "SESSION_EXPIRED")
    if device.status is not ClientDeviceStatus.ACTIVE:
        return ClientSessionAuthorityDecision(False, "DEVICE_INACTIVE")
    if ClientDeviceRole.CLIENT not in device.roles:
        return ClientSessionAuthorityDecision(False, "DEVICE_ROLE_MISSING")
    if device.human_identity_id != session.human_identity_id:
        return ClientSessionAuthorityDecision(False, "IDENTITY_MISMATCH")
    if device.device_id != session.device_id:
        return ClientSessionAuthorityDecision(False, "DEVICE_MISMATCH")
    if membership.status is not MembershipStatus.ACTIVE:
        return ClientSessionAuthorityDecision(False, "MEMBERSHIP_INACTIVE")
    if membership.human_identity_id != session.human_identity_id:
        return ClientSessionAuthorityDecision(False, "IDENTITY_MISMATCH")
    if membership.tenant_id != session.tenant_id:
        return ClientSessionAuthorityDecision(False, "TENANT_MISMATCH")
    if not tenant_active:
        return ClientSessionAuthorityDecision(False, "TENANT_INACTIVE")
    return ClientSessionAuthorityDecision(True, "AUTHORIZED")


def require_client_session_authority(
    *,
    session: ClientSessionGrant,
    device: SessionDeviceAuthority,
    membership: SessionMembershipAuthority,
    tenant_active: bool,
    now: datetime,
) -> ClientSessionAuthorityDecision:
    decision = evaluate_client_session_authority(
        session=session,
        device=device,
        membership=membership,
        tenant_active=tenant_active,
        now=now,
    )
    if not decision.allowed:
        raise ClientSessionAuthorityError(decision.reason_code)
    return decision
