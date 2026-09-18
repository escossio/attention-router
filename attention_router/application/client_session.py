"""V0.3C short client-session issuance and authenticated bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import re
import secrets

from sqlalchemy.orm import Session

from attention_router.config import Settings
from attention_router.core.client.bootstrap import (
    ClientDevicePlatform, ClientDeviceRole, ClientDeviceStatus, ClientDeviceView,
    MembershipStatus, TenantMembershipView, TenantRole,
)
from attention_router.core.client.session import (
    ClientSessionAuthorityError, ClientSessionChallenge, ClientSessionGrant,
    ClientSessionIssued, ClientSessionState, SessionDeviceAuthority,
    SessionMembershipAuthority, require_client_session_authority, resolve_session_tenant,
)
from attention_router.infrastructure import client_session_repository as repository
from attention_router.security.device_keys import (
    DeviceKeyInvalid, DeviceSignatureInvalid, decode_signature_b64url,
    encode_unpadded_base64url, parse_p256_spki_b64url, verify_p256_sha256_digest,
)

_SESSION_TOKEN = re.compile(r"^cst_[A-Za-z0-9_-]{43}$")


class ClientSessionError(Exception):
    code = "CLIENT_SESSION_UNAVAILABLE"


class ClientSessionDisabled(ClientSessionError):
    code = "CLIENT_SESSION_UNAVAILABLE"


class ClientSessionDeviceRejected(ClientSessionError):
    code = "CLIENT_SESSION_DEVICE_REJECTED"


class ClientSessionChallengeNotFound(ClientSessionError):
    code = "CLIENT_SESSION_CHALLENGE_NOT_FOUND"


class ClientSessionChallengeExpired(ClientSessionError):
    code = "CLIENT_SESSION_CHALLENGE_EXPIRED"


class ClientSessionChallengeConsumed(ClientSessionError):
    code = "CLIENT_SESSION_CHALLENGE_CONSUMED"


class ClientSessionChallengeConflict(ClientSessionError):
    code = "CLIENT_SESSION_AUTHORITY_REJECTED"


class ClientSessionSignatureInvalid(ClientSessionError):
    code = "CLIENT_SESSION_SIGNATURE_INVALID"


class ClientSessionActiveTenantRequired(ClientSessionError):
    code = "CLIENT_SESSION_ACTIVE_TENANT_REQUIRED"


class ClientSessionTenantForbidden(ClientSessionError):
    code = "CLIENT_SESSION_TENANT_FORBIDDEN"


class ClientSessionUnauthenticated(ClientSessionError):
    code = "CLIENT_SESSION_UNAUTHENTICATED"


class ClientSessionAuthorityRejected(ClientSessionError):
    code = "CLIENT_SESSION_AUTHORITY_REJECTED"


class ClientSessionUnavailable(ClientSessionError):
    code = "CLIENT_SESSION_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class AuthenticatedClientBootstrapResult:
    human_identity_id: str
    active_tenant_id: str
    memberships: tuple[TenantMembershipView, ...]
    device: ClientDeviceView
    session_expires_at: datetime
    server_time: datetime
    contract_version: str = "1"


def _aware(value: datetime, reference: datetime) -> datetime:
    return value.replace(tzinfo=reference.tzinfo or UTC) if value.tzinfo is None else value


def _membership_authority(row) -> SessionMembershipAuthority:
    return SessionMembershipAuthority(
        membership_id=row.id, human_identity_id=row.human_identity_id, tenant_id=row.tenant_id,
        role=TenantRole(row.role), status=MembershipStatus(row.status),
    )


def _device_authority(row) -> SessionDeviceAuthority:
    return SessionDeviceAuthority(
        device_id=row.id, human_identity_id=row.human_identity_id,
        status=ClientDeviceStatus(row.status),
        roles=tuple(ClientDeviceRole(role) for role in row.roles),
    )


def _membership_view(row) -> TenantMembershipView:
    return TenantMembershipView(
        membership_id=row.id, tenant_id=row.tenant_id,
        role=TenantRole(row.role), status=MembershipStatus(row.status),
    )


def _device_view(row) -> ClientDeviceView:
    return ClientDeviceView(
        device_id=row.id, public_key_fingerprint=row.public_key_fingerprint,
        canonical_name=row.canonical_name, platform=ClientDevicePlatform(row.platform),
        roles=tuple(ClientDeviceRole(role) for role in row.roles),
        status=ClientDeviceStatus(row.status),
    )


class ClientSessionService:
    def __init__(self, *, settings: Settings):
        self.settings = settings

    def _require_enabled(self) -> None:
        if not self.settings.client_session_enabled:
            raise ClientSessionDisabled()

    def start_session(self, session: Session, *, public_key_spki_b64url: str,
                      requested_tenant_id: str | None, now: datetime | None = None) -> ClientSessionChallenge:
        self._require_enabled()
        current = now if now is not None else datetime.now(UTC)
        try:
            public_key_spki, fingerprint = parse_p256_spki_b64url(public_key_spki_b64url)
        except DeviceKeyInvalid as error:
            raise ClientSessionDeviceRejected() from error
        device = repository.lock_client_device_by_fingerprint(session, fingerprint=fingerprint)
        if (
            device is None or device.status != "ACTIVE"
            or device.public_key_spki != public_key_spki or "CLIENT" not in device.roles
        ):
            raise ClientSessionDeviceRejected()
        raw = secrets.token_bytes(32)
        digest = hashlib.sha256(raw).hexdigest()
        expires_at = current + timedelta(seconds=self.settings.client_session_challenge_ttl_seconds)
        try:
            row = repository.create_session_challenge(
                session, device=device, requested_tenant_id=requested_tenant_id,
                challenge_digest=digest, now=current, expires_at=expires_at,
            )
        except repository.SessionChallengeConflict as error:
            raise ClientSessionChallengeConflict() from error
        return ClientSessionChallenge(
            session_challenge_id=row.id,
            challenge_b64url=encode_unpadded_base64url(raw),
            expires_at=expires_at,
        )

    def complete_session(self, session: Session, *, session_challenge_id: str,
                         device_signature_b64url: str, now: datetime | None = None) -> ClientSessionIssued:
        self._require_enabled()
        current = now if now is not None else datetime.now(UTC)
        observed = repository.get_session_challenge(session, session_challenge_id)
        if observed is None:
            raise ClientSessionChallengeNotFound()
        device = repository.lock_client_device_by_id(session, device_id=observed.device_id)
        row = repository.lock_session_challenge(session, session_challenge_id)
        if row is None:
            raise ClientSessionChallengeNotFound()
        if device is None or row.device_id != device.id:
            raise ClientSessionDeviceRejected()
        if row.state != "PENDING":
            raise ClientSessionChallengeConsumed()
        if _aware(row.expires_at, current) <= current:
            raise ClientSessionChallengeExpired()
        if device.status != "ACTIVE" or "CLIENT" not in device.roles or device.human_identity_id != row.human_identity_id:
            raise ClientSessionDeviceRejected()
        try:
            signature = decode_signature_b64url(device_signature_b64url)
            verify_p256_sha256_digest(
                public_key_spki=device.public_key_spki,
                challenge_digest_hex=row.challenge_digest,
                signature=signature,
            )
        except DeviceSignatureInvalid as error:
            raise ClientSessionSignatureInvalid() from error

        memberships = repository.lock_memberships_for_human(session, human_identity_id=row.human_identity_id)
        authorities = tuple(_membership_authority(item) for item in memberships)
        try:
            tenant_id = resolve_session_tenant(authorities, requested_tenant_id=row.requested_tenant_id)
        except ClientSessionAuthorityError as error:
            if error.reason_code == "ACTIVE_TENANT_REQUIRED":
                raise ClientSessionActiveTenantRequired() from error
            raise ClientSessionTenantForbidden() from error
        selected = [item for item in memberships if item.tenant_id == tenant_id and item.status == "ACTIVE"]
        if len(selected) != 1:
            raise ClientSessionTenantForbidden()
        tenant = repository.lock_tenant(session, tenant_id=tenant_id)
        if tenant is None or tenant.status != "ACTIVE":
            raise ClientSessionTenantForbidden()

        raw_token = "cst_" + secrets.token_urlsafe(32)
        if not _SESSION_TOKEN.fullmatch(raw_token):
            raise ClientSessionUnavailable()
        token_digest = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        expires_at = current + timedelta(seconds=self.settings.client_session_ttl_seconds)
        try:
            session_row = repository.create_client_session(
                session, source_challenge_id=row.id, token_digest=token_digest,
                human_identity_id=row.human_identity_id, device_id=device.id,
                tenant_id=tenant_id, now=current, expires_at=expires_at,
            )
            repository.mark_session_challenge_verified(row, now=current)
            session.flush()
        except (repository.SessionIssueConflict, repository.SessionChallengeConflict) as error:
            raise ClientSessionChallengeConsumed() from error
        return ClientSessionIssued(
            session_id=session_row.id, session_token=raw_token, expires_at=expires_at,
            human_identity_id=row.human_identity_id, device_id=device.id, tenant_id=tenant_id,
        )

    def authenticated_bootstrap(self, session: Session, *, session_token: str | None,
                                now: datetime | None = None) -> AuthenticatedClientBootstrapResult:
        self._require_enabled()
        current = now if now is not None else datetime.now(UTC)
        if not isinstance(session_token, str) or not _SESSION_TOKEN.fullmatch(session_token):
            raise ClientSessionUnauthenticated()
        session_row = repository.get_client_session_by_token(session, token=session_token)
        if session_row is None or session_row.state != "ACTIVE" or _aware(session_row.expires_at, current) <= current:
            raise ClientSessionUnauthenticated()
        device = repository.get_client_device(session, device_id=session_row.device_id)
        membership = repository.get_membership(
            session, human_identity_id=session_row.human_identity_id, tenant_id=session_row.tenant_id
        )
        tenant = repository.get_tenant(session, tenant_id=session_row.tenant_id)
        if device is None or membership is None or tenant is None:
            raise ClientSessionAuthorityRejected()
        grant = ClientSessionGrant(
            session_id=session_row.id, human_identity_id=session_row.human_identity_id,
            device_id=session_row.device_id, tenant_id=session_row.tenant_id,
            state=ClientSessionState(session_row.state),
            expires_at=_aware(session_row.expires_at, current),
        )
        try:
            require_client_session_authority(
                session=grant, device=_device_authority(device),
                membership=_membership_authority(membership),
                tenant_active=tenant.status == "ACTIVE", now=current,
            )
        except (ValueError, ClientSessionAuthorityError) as error:
            raise ClientSessionAuthorityRejected() from error
        memberships = repository.list_active_memberships(session, human_identity_id=session_row.human_identity_id)
        if not any(item.tenant_id == session_row.tenant_id for item in memberships):
            raise ClientSessionAuthorityRejected()
        return AuthenticatedClientBootstrapResult(
            human_identity_id=session_row.human_identity_id,
            active_tenant_id=session_row.tenant_id,
            memberships=tuple(_membership_view(item) for item in memberships),
            device=_device_view(device),
            session_expires_at=_aware(session_row.expires_at, current),
            server_time=current,
        )
