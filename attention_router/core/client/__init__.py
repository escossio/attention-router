"""Pure client authority vocabulary for pre-session bootstrap flows."""

from .bootstrap import (
    ClientDevicePlatform,
    ClientDeviceRole,
    ClientDeviceStatus,
    ClientDeviceView,
    DeviceBootstrapChallenge,
    DeviceBootstrapEstablished,
    MembershipStatus,
    TenantMembershipView,
    TenantRole,
)

__all__ = [
    "ClientDevicePlatform",
    "ClientDeviceRole",
    "ClientDeviceStatus",
    "ClientDeviceView",
    "DeviceBootstrapChallenge",
    "DeviceBootstrapEstablished",
    "MembershipStatus",
    "TenantMembershipView",
    "TenantRole",
]

from .session import (
    ClientSessionAuthorityDecision,
    ClientSessionAuthorityError,
    ClientSessionChallenge,
    ClientSessionGrant,
    ClientSessionIssued,
    ClientSessionState,
    SessionDeviceAuthority,
    SessionMembershipAuthority,
    evaluate_client_session_authority,
    require_client_session_authority,
    resolve_session_tenant,
)

__all__ += [
    "ClientSessionAuthorityDecision",
    "ClientSessionAuthorityError",
    "ClientSessionChallenge",
    "ClientSessionGrant",
    "ClientSessionIssued",
    "ClientSessionState",
    "SessionDeviceAuthority",
    "SessionMembershipAuthority",
    "evaluate_client_session_authority",
    "require_client_session_authority",
    "resolve_session_tenant",
]
