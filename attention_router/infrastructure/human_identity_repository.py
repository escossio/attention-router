"""Human Identity persistence within caller-owned transactions."""

from datetime import datetime
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from attention_router.core.human_identity import HumanAuthChallengeConsumed
from attention_router.infrastructure.human_identity_models import (
    ExternalIdentityBindingRow,
    HumanAuthTransactionRow,
    HumanIdentityRow,
)


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
