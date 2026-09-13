"""Single-use Human Identity challenges within caller-owned transactions."""

from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import secrets
from typing import Protocol

from sqlalchemy.orm import Session

from attention_router.config import Settings
from attention_router.core.human_identity import (
    HumanAuthChallengeConsumed,
    HumanAuthChallengeExpired,
    HumanAuthChallengeNotFound,
    HumanAuthCredentialRejected,
    HumanAuthDisabled,
    HumanAuthNonceMismatch,
    HumanIdentityValidated,
    IssuedHumanAuthChallenge,
    VerifiedProviderIdentity,
)
from attention_router.infrastructure.human_identity_models import HumanAuthTransactionRow
from attention_router.infrastructure.human_identity_repository import (
    create_human_auth_transaction,
    get_human_auth_transaction,
    lock_human_auth_transaction,
    mark_human_auth_rejected,
    mark_human_auth_verified,
    resolve_human_identity,
)


class _IdentityVerifier(Protocol):
    def verify(self, id_token: str) -> VerifiedProviderIdentity: ...


def _require_pending(
    row: HumanAuthTransactionRow | None, now: datetime,
) -> HumanAuthTransactionRow:
    if row is None:
        raise HumanAuthChallengeNotFound()
    if row.state != "PENDING":
        raise HumanAuthChallengeConsumed()
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        raise HumanAuthChallengeExpired()
    return row


class HumanIdentityService:
    def __init__(self, *, settings: Settings, verifier: _IdentityVerifier):
        self.settings = settings
        self.verifier = verifier

    def issue_google_challenge(
        self, session: Session, now: datetime | None = None,
    ) -> IssuedHumanAuthChallenge:
        if not self.settings.human_identity_enabled:
            raise HumanAuthDisabled()
        current_time = now if now is not None else datetime.now(UTC)
        challenge_id = f"hac_{secrets.token_urlsafe(24)}"
        nonce = secrets.token_urlsafe(32)
        nonce_digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        expires_at = current_time + timedelta(
            seconds=self.settings.human_auth_challenge_ttl_seconds
        )
        create_human_auth_transaction(
            session, transaction_id=challenge_id, provider="google",
            nonce_digest=nonce_digest, now=current_time, expires_at=expires_at,
        )
        return IssuedHumanAuthChallenge(
            challenge_id=challenge_id, nonce=nonce, expires_at=expires_at,
        )

    def verify_google_challenge(
        self, session: Session, challenge_id: str, id_token: str,
        now: datetime | None = None,
    ) -> HumanIdentityValidated:
        if not self.settings.human_identity_enabled:
            raise HumanAuthDisabled()
        row = get_human_auth_transaction(session, challenge_id)
        _require_pending(row, now if now is not None else datetime.now(UTC))

        try:
            verified = self.verifier.verify(id_token)
        except HumanAuthCredentialRejected:
            locked = lock_human_auth_transaction(session, challenge_id)
            locked_now = now if now is not None else datetime.now(UTC)
            locked = _require_pending(locked, locked_now)
            mark_human_auth_rejected(locked, now=locked_now)
            raise

        locked = lock_human_auth_transaction(session, challenge_id)
        locked_now = now if now is not None else datetime.now(UTC)
        locked = _require_pending(locked, locked_now)
        verified_nonce_digest = hashlib.sha256(verified.nonce.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(verified_nonce_digest, locked.nonce_digest):
            mark_human_auth_rejected(locked, now=locked_now)
            raise HumanAuthNonceMismatch()
        human_identity_id = resolve_human_identity(
            session, provider=verified.provider, subject=verified.subject, now=locked_now,
        )
        mark_human_auth_verified(locked, human_identity_id=human_identity_id, now=locked_now)
        return HumanIdentityValidated(human_identity_id=human_identity_id)
