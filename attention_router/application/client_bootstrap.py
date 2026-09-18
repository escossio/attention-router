"""V0.3B pre-session device-bootstrap authority service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import re
import secrets

from sqlalchemy.orm import Session

from attention_router.config import Settings
from attention_router.core.client.bootstrap import (
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
from attention_router.infrastructure import client_bootstrap_repository as repository
from attention_router.security.device_keys import (
    DeviceKeyInvalid,
    DeviceSignatureInvalid,
    decode_signature_b64url,
    encode_unpadded_base64url,
    parse_p256_spki_b64url,
    verify_p256_sha256_digest,
)


_CONTINUATION = re.compile(r"^hcg_[A-Za-z0-9_-]{43}$")
_ALLOWED_ROLES = {"CLIENT", "CAPABILITY_NODE"}


class DeviceBootstrapError(Exception):
    code = "DEVICE_BOOTSTRAP_UNAVAILABLE"


class DeviceBootstrapDisabled(DeviceBootstrapError):
    code = "DEVICE_BOOTSTRAP_UNAVAILABLE"


class DeviceBootstrapGrantRejected(DeviceBootstrapError):
    code = "DEVICE_BOOTSTRAP_GRANT_REJECTED"


class DeviceBootstrapChallengeNotFound(DeviceBootstrapError):
    code = "DEVICE_BOOTSTRAP_CHALLENGE_NOT_FOUND"


class DeviceBootstrapChallengeExpired(DeviceBootstrapError):
    code = "DEVICE_BOOTSTRAP_CHALLENGE_EXPIRED"


class DeviceBootstrapChallengeConsumed(DeviceBootstrapError):
    code = "DEVICE_BOOTSTRAP_CHALLENGE_CONSUMED"


class DeviceBootstrapDeviceKeyInvalid(DeviceBootstrapError):
    code = "DEVICE_BOOTSTRAP_DEVICE_KEY_INVALID"


class DeviceBootstrapSignatureInvalid(DeviceBootstrapError):
    code = "DEVICE_BOOTSTRAP_SIGNATURE_INVALID"


class DeviceBootstrapMembershipConflict(DeviceBootstrapError):
    code = "DEVICE_BOOTSTRAP_MEMBERSHIP_CONFLICT"


class DeviceBootstrapDeviceConflict(DeviceBootstrapError):
    code = "DEVICE_BOOTSTRAP_DEVICE_CONFLICT"


class DeviceBootstrapUnavailable(DeviceBootstrapError):
    code = "DEVICE_BOOTSTRAP_UNAVAILABLE"


def _aware(value: datetime, reference: datetime) -> datetime:
    return value.replace(tzinfo=reference.tzinfo or UTC) if value.tzinfo is None else value


def _membership_view(row) -> TenantMembershipView:
    return TenantMembershipView(
        membership_id=row.id,
        tenant_id=row.tenant_id,
        role=TenantRole(row.role),
        status=MembershipStatus(row.status),
    )


def _device_view(row) -> ClientDeviceView:
    return ClientDeviceView(
        device_id=row.id,
        public_key_fingerprint=row.public_key_fingerprint,
        canonical_name=row.canonical_name,
        platform=ClientDevicePlatform(row.platform),
        roles=tuple(ClientDeviceRole(role) for role in row.roles),
        status=ClientDeviceStatus(row.status),
    )


class DeviceBootstrapService:
    def __init__(self, *, settings: Settings):
        self.settings = settings

    def _require_enabled(self) -> None:
        if not self.settings.device_bootstrap_enabled:
            raise DeviceBootstrapDisabled()

    def start_device_bootstrap(
        self,
        session: Session,
        *,
        continuation_token: str,
        public_key_spki_b64url: str,
        canonical_device_name: str,
        platform: str,
        roles: list[str],
        now: datetime | None = None,
    ) -> DeviceBootstrapChallenge:
        self._require_enabled()
        current = now if now is not None else datetime.now(UTC)

        if (
            not isinstance(continuation_token, str)
            or not _CONTINUATION.fullmatch(continuation_token)
        ):
            raise DeviceBootstrapGrantRejected()
        if (
            platform != "ANDROID"
            or not isinstance(canonical_device_name, str)
            or not 1 <= len(canonical_device_name) <= 160
            or not roles
            or len(roles) > 2
            or len(set(roles)) != len(roles)
            or not set(roles) <= _ALLOWED_ROLES
        ):
            raise DeviceBootstrapDeviceKeyInvalid()

        try:
            public_key_spki, fingerprint = parse_p256_spki_b64url(
                public_key_spki_b64url
            )
        except DeviceKeyInvalid as error:
            raise DeviceBootstrapDeviceKeyInvalid() from error

        grant = repository.lock_continuation_grant_by_token(
            session,
            token=continuation_token,
            now=current,
        )
        if grant is None:
            raise DeviceBootstrapGrantRejected()

        raw_challenge = secrets.token_bytes(32)
        challenge_digest = hashlib.sha256(raw_challenge).hexdigest()
        expires_at = current + timedelta(
            seconds=self.settings.device_bootstrap_challenge_ttl_seconds
        )
        try:
            row = repository.create_or_refresh_bootstrap_challenge(
                session,
                continuation_grant=grant,
                public_key_fingerprint=fingerprint,
                public_key_spki=public_key_spki,
                canonical_device_name=canonical_device_name,
                platform=platform,
                roles=sorted(roles),
                challenge_digest=challenge_digest,
                now=current,
                expires_at=expires_at,
            )
        except repository.BootstrapChallengeConflict as error:
            raise DeviceBootstrapChallengeConsumed() from error

        return DeviceBootstrapChallenge(
            bootstrap_challenge_id=row.id,
            challenge_b64url=encode_unpadded_base64url(raw_challenge),
            expires_at=expires_at,
        )

    def complete_device_bootstrap(
        self,
        session: Session,
        *,
        bootstrap_challenge_id: str,
        device_signature_b64url: str,
        now: datetime | None = None,
    ) -> DeviceBootstrapEstablished:
        self._require_enabled()
        current = now if now is not None else datetime.now(UTC)

        observed = repository.get_bootstrap_challenge(
            session,
            bootstrap_challenge_id,
        )
        if observed is None:
            raise DeviceBootstrapChallengeNotFound()

        # Lock order is grant -> challenge in both start and complete paths.
        grant = repository.lock_continuation_grant_by_id(
            session,
            grant_id=observed.continuation_grant_id,
        )
        row = repository.lock_bootstrap_challenge(
            session,
            bootstrap_challenge_id,
        )
        if row is None:
            raise DeviceBootstrapChallengeNotFound()
        if row.continuation_grant_id != observed.continuation_grant_id:
            raise DeviceBootstrapUnavailable()
        if row.state != "PENDING":
            raise DeviceBootstrapChallengeConsumed()
        if _aware(row.expires_at, current) <= current:
            raise DeviceBootstrapChallengeExpired()
        if grant is None or not repository.continuation_grant_is_usable(
            grant,
            human_identity_id=row.human_identity_id,
            now=current,
        ):
            raise DeviceBootstrapGrantRejected()

        try:
            signature = decode_signature_b64url(device_signature_b64url)
            verify_p256_sha256_digest(
                public_key_spki=row.public_key_spki,
                challenge_digest_hex=row.challenge_digest,
                signature=signature,
            )
        except DeviceSignatureInvalid as error:
            raise DeviceBootstrapSignatureInvalid() from error

        try:
            memberships = repository.resolve_or_create_memberships(
                session,
                human_identity_id=row.human_identity_id,
                now=current,
            )
        except repository.MembershipResolutionConflict as error:
            raise DeviceBootstrapMembershipConflict() from error

        try:
            device = repository.create_or_recover_client_device(
                session,
                human_identity_id=row.human_identity_id,
                public_key_fingerprint=row.public_key_fingerprint,
                public_key_spki=row.public_key_spki,
                canonical_name=row.canonical_device_name,
                platform=row.platform,
                roles=list(row.roles),
                now=current,
            )
        except repository.DeviceResolutionConflict as error:
            raise DeviceBootstrapDeviceConflict() from error

        repository.mark_bootstrap_verified(row, now=current)
        repository.consume_locked_continuation_grant(grant, now=current)
        session.flush()

        active = tuple(_membership_view(item) for item in memberships)
        initial_tenant_id = active[0].tenant_id if len(active) == 1 else None
        return DeviceBootstrapEstablished(
            human_identity_id=row.human_identity_id,
            memberships=active,
            initial_tenant_id=initial_tenant_id,
            device=_device_view(device),
        )
