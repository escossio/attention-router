"""Caller-transaction-owned persistence for V0.3C client sessions."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import hmac
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from attention_router.infrastructure.client_bootstrap_models import ClientDeviceRow, ClientTenantMembershipRow
from attention_router.infrastructure.client_session_models import ClientSessionChallengeRow, ClientSessionRow
from attention_router.infrastructure.models import TenantRow


class SessionChallengeConflict(RuntimeError):
    pass


class SessionIssueConflict(RuntimeError):
    pass


def _aware(value: datetime, reference: datetime) -> datetime:
    return value.replace(tzinfo=reference.tzinfo or UTC) if value.tzinfo is None else value


def lock_client_device_by_fingerprint(session: Session, *, fingerprint: str) -> ClientDeviceRow | None:
    return session.scalar(
        select(ClientDeviceRow)
        .where(ClientDeviceRow.public_key_fingerprint == fingerprint)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def lock_client_device_by_id(session: Session, *, device_id: str) -> ClientDeviceRow | None:
    return session.scalar(
        select(ClientDeviceRow)
        .where(ClientDeviceRow.id == device_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def create_session_challenge(
    session: Session,
    *,
    device: ClientDeviceRow,
    requested_tenant_id: str | None,
    challenge_digest: str,
    now: datetime,
    expires_at: datetime,
) -> ClientSessionChallengeRow:
    pending = list(session.scalars(
        select(ClientSessionChallengeRow)
        .where(ClientSessionChallengeRow.device_id == device.id, ClientSessionChallengeRow.state == "PENDING")
        .order_by(ClientSessionChallengeRow.created_at, ClientSessionChallengeRow.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all())
    for existing in pending:
        if _aware(existing.expires_at, now) > now:
            raise SessionChallengeConflict()
        existing.state = "REJECTED"
        existing.rejected_at = now

    row = ClientSessionChallengeRow(
        id="csc_" + secrets.token_urlsafe(24),
        device_id=device.id,
        human_identity_id=device.human_identity_id,
        requested_tenant_id=requested_tenant_id,
        challenge_digest=challenge_digest,
        state="PENDING",
        created_at=now,
        expires_at=expires_at,
        verified_at=None,
        rejected_at=None,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as error:
        raise SessionChallengeConflict() from error
    return row


def get_session_challenge(session: Session, challenge_id: str) -> ClientSessionChallengeRow | None:
    return session.get(ClientSessionChallengeRow, challenge_id)


def lock_session_challenge(session: Session, challenge_id: str) -> ClientSessionChallengeRow | None:
    return session.scalar(
        select(ClientSessionChallengeRow)
        .where(ClientSessionChallengeRow.id == challenge_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def lock_memberships_for_human(session: Session, *, human_identity_id: str) -> list[ClientTenantMembershipRow]:
    return list(session.scalars(
        select(ClientTenantMembershipRow)
        .where(ClientTenantMembershipRow.human_identity_id == human_identity_id)
        .order_by(ClientTenantMembershipRow.created_at, ClientTenantMembershipRow.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all())


def lock_tenant(session: Session, *, tenant_id: str) -> TenantRow | None:
    return session.scalar(
        select(TenantRow).where(TenantRow.id == tenant_id).with_for_update().execution_options(populate_existing=True)
    )


def create_client_session(
    session: Session,
    *,
    source_challenge_id: str,
    token_digest: str,
    human_identity_id: str,
    device_id: str,
    tenant_id: str,
    now: datetime,
    expires_at: datetime,
) -> ClientSessionRow:
    row = ClientSessionRow(
        id="csn_" + secrets.token_urlsafe(24),
        token_digest=token_digest,
        source_challenge_id=source_challenge_id,
        human_identity_id=human_identity_id,
        device_id=device_id,
        tenant_id=tenant_id,
        state="ACTIVE",
        created_at=now,
        expires_at=expires_at,
        revoked_at=None,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as error:
        raise SessionIssueConflict() from error
    return row


def mark_session_challenge_verified(row: ClientSessionChallengeRow, *, now: datetime) -> None:
    if row.state != "PENDING":
        raise SessionChallengeConflict()
    row.state = "VERIFIED"
    row.verified_at = now


def get_client_session_by_token(session: Session, *, token: str) -> ClientSessionRow | None:
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    row = session.scalar(select(ClientSessionRow).where(ClientSessionRow.token_digest == digest))
    if row is None or not hmac.compare_digest(row.token_digest, digest):
        return None
    return row


def get_client_device(session: Session, *, device_id: str) -> ClientDeviceRow | None:
    return session.get(ClientDeviceRow, device_id)


def get_membership(session: Session, *, human_identity_id: str, tenant_id: str) -> ClientTenantMembershipRow | None:
    return session.scalar(select(ClientTenantMembershipRow).where(
        ClientTenantMembershipRow.human_identity_id == human_identity_id,
        ClientTenantMembershipRow.tenant_id == tenant_id,
    ))


def get_tenant(session: Session, *, tenant_id: str) -> TenantRow | None:
    return session.get(TenantRow, tenant_id)


def list_active_memberships(session: Session, *, human_identity_id: str) -> list[ClientTenantMembershipRow]:
    return list(session.scalars(
        select(ClientTenantMembershipRow)
        .where(
            ClientTenantMembershipRow.human_identity_id == human_identity_id,
            ClientTenantMembershipRow.status == "ACTIVE",
        )
        .order_by(ClientTenantMembershipRow.created_at, ClientTenantMembershipRow.id)
    ).all())
