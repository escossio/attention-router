"""Human Identity persistence within caller-owned transactions."""

from datetime import datetime
import secrets
import hashlib
import hmac

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from attention_router.core.human_identity import HumanAuthChallengeConsumed
from attention_router.infrastructure.human_identity_models import (
    ExternalIdentityBindingRow,
    HumanAuthTransactionRow,
    HumanAuthContinuationGrantRow,
    HumanIdentityRow,
)


def create_human_auth_continuation_grant(
    session: Session, *, human_identity_id: str, source_auth_transaction_id: str,
    now: datetime, expires_at: datetime,
) -> str:
    token = "hcg_" + secrets.token_urlsafe(32)
    row = HumanAuthContinuationGrantRow(
        id="hcgi_" + secrets.token_urlsafe(24),
        token_digest=hashlib.sha256(token.encode("ascii")).hexdigest(),
        human_identity_id=human_identity_id,
        source_auth_transaction_id=source_auth_transaction_id,
        purpose="DEVICE_BOOTSTRAP",
        state="ACTIVE",
        created_at=now,
        expires_at=expires_at,
    )
    session.add(row)
    session.flush()
    return token


def consume_human_auth_continuation_grant(
    session: Session, *, token_digest: str, expected_human_identity_id: str,
    purpose: str, now: datetime,
) -> HumanAuthContinuationGrantRow | None:
    row = session.scalar(
        select(HumanAuthContinuationGrantRow).where(
            HumanAuthContinuationGrantRow.token_digest == token_digest
        ).with_for_update().execution_options(populate_existing=True)
    )
    if row is None or not hmac.compare_digest(row.token_digest, token_digest):
        return None
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=now.tzinfo)
    if (
        row.state != "ACTIVE"
        or expires_at <= now
        or row.purpose != purpose
        or row.human_identity_id != expected_human_identity_id
    ):
        return None
    row.state = "CONSUMED"
    row.consumed_at = now
    session.flush()
    return row


def revoke_human_auth_continuation_grant(
    session: Session, *, token_digest: str, expected_human_identity_id: str,
    now: datetime,
) -> bool:
    row = session.scalar(
        select(HumanAuthContinuationGrantRow).where(
            HumanAuthContinuationGrantRow.token_digest == token_digest
        ).with_for_update().execution_options(populate_existing=True)
    )
    if row is None or row.state != "ACTIVE" or row.human_identity_id != expected_human_identity_id:
        return False
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=now.tzinfo)
    if expires_at <= now:
        return False
    row.state = "REVOKED"
    row.revoked_at = now
    session.flush()
    return True


def create_human_auth_transaction(
    session: Session,
    *,
    transaction_id: str,
    provider: str,
    nonce_digest: str,
    now: datetime,
    expires_at: datetime,
) -> HumanAuthTransactionRow:
    row = HumanAuthTransactionRow(
        id=transaction_id,
        provider=provider,
        nonce_digest=nonce_digest,
        state="PENDING",
        created_at=now,
        expires_at=expires_at,
        consumed_at=None,
        resolved_human_identity_id=None,
    )
    session.add(row)
    session.flush()
    return row


def get_human_auth_transaction(
    session: Session, transaction_id: str,
) -> HumanAuthTransactionRow | None:
    return session.get(HumanAuthTransactionRow, transaction_id)


def lock_human_auth_transaction(
    session: Session, transaction_id: str,
) -> HumanAuthTransactionRow | None:
    return session.scalar(
        select(HumanAuthTransactionRow).where(
            HumanAuthTransactionRow.id == transaction_id
        ).with_for_update().execution_options(populate_existing=True)
    )


def resolve_human_identity(
    session: Session, *, provider: str, subject: str, now: datetime,
) -> str:
    binding_query = select(ExternalIdentityBindingRow).where(
        ExternalIdentityBindingRow.provider == provider,
        ExternalIdentityBindingRow.subject == subject,
    )
    binding = session.scalar(binding_query)
    if binding is not None:
        binding.last_verified_at = now
        return binding.human_identity_id

    human_identity_id = "hid_" + secrets.token_urlsafe(24)
    try:
        with session.begin_nested():
            session.add(HumanIdentityRow(id=human_identity_id, created_at=now))
            # These mappers have no relationship to order their inserts for the FK.
            session.flush()
            session.add(ExternalIdentityBindingRow(
                id=secrets.token_urlsafe(24),
                provider=provider,
                subject=subject,
                human_identity_id=human_identity_id,
                created_at=now,
                last_verified_at=now,
            ))
            session.flush()
    except IntegrityError as error:
        diagnostic = getattr(error.orig, "diag", None)
        if (
            getattr(error.orig, "sqlstate", None) != "23505"
            or getattr(diagnostic, "constraint_name", None)
            != "uq_external_identity_provider_subject"
        ):
            raise
        binding = session.scalar(binding_query)
        if binding is None:
            raise RuntimeError("Human Identity binding missing after unique conflict") from error
        return binding.human_identity_id
    return human_identity_id


def mark_human_auth_verified(
    row: HumanAuthTransactionRow, *, human_identity_id: str, now: datetime,
) -> None:
    if row.state != "PENDING":
        raise HumanAuthChallengeConsumed()
    row.state = "VERIFIED"
    row.consumed_at = now
    row.resolved_human_identity_id = human_identity_id


def mark_human_auth_rejected(row: HumanAuthTransactionRow, *, now: datetime) -> None:
    if row.state != "PENDING":
        raise HumanAuthChallengeConsumed()
    row.state = "REJECTED"
    row.consumed_at = now
    row.resolved_human_identity_id = None
