"""Caller-transaction-owned persistence for V0.3B device bootstrap."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import hmac
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from attention_router.infrastructure.client_bootstrap_models import (
    ClientDeviceRow,
    ClientTenantMembershipRow,
    DeviceBootstrapChallengeRow,
)
from attention_router.infrastructure.human_identity_models import (
    HumanAuthContinuationGrantRow,
    HumanIdentityRow,
)
from attention_router.infrastructure.models import TenantRow


class BootstrapChallengeConflict(RuntimeError):
    pass


class MembershipResolutionConflict(RuntimeError):
    pass


class DeviceResolutionConflict(RuntimeError):
    pass


def _aware(value: datetime, reference: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=reference.tzinfo or UTC)
    return value


def continuation_grant_is_usable(
    row: HumanAuthContinuationGrantRow,
    *,
    human_identity_id: str | None,
    now: datetime,
) -> bool:
    return (
        row.state == "ACTIVE"
        and row.purpose == "DEVICE_BOOTSTRAP"
        and _aware(row.expires_at, now) > now
        and (human_identity_id is None or row.human_identity_id == human_identity_id)
    )


def lock_continuation_grant_by_token(
    session: Session,
    *,
    token: str,
    now: datetime,
) -> HumanAuthContinuationGrantRow | None:
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    row = session.scalar(
        select(HumanAuthContinuationGrantRow)
        .where(HumanAuthContinuationGrantRow.token_digest == digest)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or not hmac.compare_digest(row.token_digest, digest):
        return None
    return row if continuation_grant_is_usable(row, human_identity_id=None, now=now) else None


def lock_continuation_grant_by_id(
    session: Session,
    *,
    grant_id: str,
) -> HumanAuthContinuationGrantRow | None:
    return session.scalar(
        select(HumanAuthContinuationGrantRow)
        .where(HumanAuthContinuationGrantRow.id == grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def create_or_refresh_bootstrap_challenge(
    session: Session,
    *,
    continuation_grant: HumanAuthContinuationGrantRow,
    public_key_fingerprint: str,
    public_key_spki: bytes,
    canonical_device_name: str,
    platform: str,
    roles: list[str],
    challenge_digest: str,
    now: datetime,
    expires_at: datetime,
) -> DeviceBootstrapChallengeRow:
    existing = session.scalar(
        select(DeviceBootstrapChallengeRow)
        .where(
            DeviceBootstrapChallengeRow.continuation_grant_id == continuation_grant.id
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        if existing.state == "VERIFIED":
            raise BootstrapChallengeConflict()
        if existing.state == "PENDING" and _aware(existing.expires_at, now) > now:
            raise BootstrapChallengeConflict()
        existing.public_key_fingerprint = public_key_fingerprint
        existing.public_key_spki = public_key_spki
        existing.canonical_device_name = canonical_device_name
        existing.platform = platform
        existing.roles = roles
        existing.challenge_digest = challenge_digest
        existing.state = "PENDING"
        existing.created_at = now
        existing.expires_at = expires_at
        existing.verified_at = None
        existing.rejected_at = None
        session.flush()
        return existing

    row = DeviceBootstrapChallengeRow(
        id="dbc_" + secrets.token_urlsafe(24),
        continuation_grant_id=continuation_grant.id,
        human_identity_id=continuation_grant.human_identity_id,
        public_key_fingerprint=public_key_fingerprint,
        public_key_spki=public_key_spki,
        canonical_device_name=canonical_device_name,
        platform=platform,
        roles=roles,
        challenge_digest=challenge_digest,
        state="PENDING",
        created_at=now,
        expires_at=expires_at,
        verified_at=None,
        rejected_at=None,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as error:
        raise BootstrapChallengeConflict() from error
    return row


def get_bootstrap_challenge(
    session: Session,
    challenge_id: str,
) -> DeviceBootstrapChallengeRow | None:
    return session.get(DeviceBootstrapChallengeRow, challenge_id)


def lock_bootstrap_challenge(
    session: Session,
    challenge_id: str,
) -> DeviceBootstrapChallengeRow | None:
    return session.scalar(
        select(DeviceBootstrapChallengeRow)
        .where(DeviceBootstrapChallengeRow.id == challenge_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def list_active_memberships(
    session: Session,
    human_identity_id: str,
) -> list[ClientTenantMembershipRow]:
    return list(
        session.scalars(
            select(ClientTenantMembershipRow)
            .where(
                ClientTenantMembershipRow.human_identity_id == human_identity_id,
                ClientTenantMembershipRow.status == "ACTIVE",
            )
            .order_by(ClientTenantMembershipRow.created_at, ClientTenantMembershipRow.id)
        ).all()
    )


def resolve_or_create_memberships(
    session: Session,
    *,
    human_identity_id: str,
    now: datetime,
) -> list[ClientTenantMembershipRow]:
    human = session.scalar(
        select(HumanIdentityRow)
        .where(HumanIdentityRow.id == human_identity_id)
        .with_for_update()
    )
    if human is None:
        raise MembershipResolutionConflict()

    memberships = list_active_memberships(session, human_identity_id)
    if memberships:
        return memberships

    tenant_id = "tnt_" + secrets.token_urlsafe(24)
    slug = "personal-" + secrets.token_urlsafe(12).lower().replace("_", "-")
    tenant = TenantRow(
        id=tenant_id,
        slug=slug,
        name="Personal",
        status="ACTIVE",
        created_at=now,
        updated_at=now,
    )
    membership = ClientTenantMembershipRow(
        id="ctm_" + secrets.token_urlsafe(24),
        human_identity_id=human_identity_id,
        tenant_id=tenant_id,
        role="OWNER",
        status="ACTIVE",
        created_at=now,
        updated_at=now,
    )
    session.add(tenant)
    session.flush()
    session.add(membership)
    try:
        session.flush()
    except IntegrityError as error:
        raise MembershipResolutionConflict() from error
    return [membership]


def create_or_recover_client_device(
    session: Session,
    *,
    human_identity_id: str,
    public_key_fingerprint: str,
    public_key_spki: bytes,
    canonical_name: str,
    platform: str,
    roles: list[str],
    now: datetime,
) -> ClientDeviceRow:
    existing = session.scalar(
        select(ClientDeviceRow)
        .where(ClientDeviceRow.public_key_fingerprint == public_key_fingerprint)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    normalized_roles = sorted(set(roles))
    if existing is not None:
        if (
            existing.human_identity_id != human_identity_id
            or existing.status != "ACTIVE"
            or existing.public_key_spki != public_key_spki
            or existing.platform != platform
            or sorted(existing.roles) != normalized_roles
        ):
            raise DeviceResolutionConflict()
        existing.canonical_name = canonical_name
        existing.updated_at = now
        session.flush()
        return existing

    row = ClientDeviceRow(
        id="cdev_" + secrets.token_urlsafe(24),
        human_identity_id=human_identity_id,
        public_key_fingerprint=public_key_fingerprint,
        public_key_spki=public_key_spki,
        canonical_name=canonical_name,
        platform=platform,
        roles=normalized_roles,
        status="ACTIVE",
        created_at=now,
        updated_at=now,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as error:
        existing = session.scalar(
            select(ClientDeviceRow)
            .where(ClientDeviceRow.public_key_fingerprint == public_key_fingerprint)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            existing is None
            or existing.human_identity_id != human_identity_id
            or existing.status != "ACTIVE"
            or existing.public_key_spki != public_key_spki
            or existing.platform != platform
            or sorted(existing.roles) != normalized_roles
        ):
            raise DeviceResolutionConflict() from error
        return existing
    return row


def mark_bootstrap_verified(
    row: DeviceBootstrapChallengeRow,
    *,
    now: datetime,
) -> None:
    if row.state != "PENDING":
        raise BootstrapChallengeConflict()
    row.state = "VERIFIED"
    row.verified_at = now


def consume_locked_continuation_grant(
    row: HumanAuthContinuationGrantRow,
    *,
    now: datetime,
) -> None:
    if not continuation_grant_is_usable(
        row,
        human_identity_id=row.human_identity_id,
        now=now,
    ):
        raise BootstrapChallengeConflict()
    row.state = "CONSUMED"
    row.consumed_at = now
