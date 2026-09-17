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
    HumanIdentityContinued,
    HumanAuthContinuationGrant,
    HumanAuthContinuationGrantRejected,
    IssuedHumanAuthChallenge,
    VerifiedProviderIdentity,
)
from attention_router.infrastructure.human_identity_models import HumanAuthTransactionRow
from attention_router.infrastructure.human_identity_repository import (
    create_human_auth_transaction,
    create_human_auth_continuation_grant,
    consume_human_auth_continuation_grant,
    revoke_human_auth_continuation_grant,
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

    def verify_google_challenge_and_issue_continuation_grant(
        self, session: Session, challenge_id: str, id_token: str,
        now: datetime | None = None,
    ) -> HumanIdentityContinued:
        if not self.settings.human_identity_enabled:
            raise HumanAuthDisabled()
        row = get_human_auth_transaction(session, challenge_id)
        current_time = now if now is not None else datetime.now(UTC)
        _require_pending(row, current_time)
        try:
            verified = self.verifier.verify(id_token)
        except HumanAuthCredentialRejected:
            locked = lock_human_auth_transaction(session, challenge_id)
            locked = _require_pending(locked, now if now is not None else datetime.now(UTC))
            mark_human_auth_rejected(locked, now=now if now is not None else datetime.now(UTC))
            raise
        locked_now = now if now is not None else datetime.now(UTC)
        locked = _require_pending(lock_human_auth_transaction(session, challenge_id), locked_now)
        nonce_digest = hashlib.sha256(verified.nonce.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(nonce_digest, locked.nonce_digest):
            mark_human_auth_rejected(locked, now=locked_now)
            raise HumanAuthNonceMismatch()
        human_identity_id = resolve_human_identity(
            session, provider=verified.provider, subject=verified.subject, now=locked_now,
        )
        mark_human_auth_verified(locked, human_identity_id=human_identity_id, now=locked_now)
        expires_at = locked_now + timedelta(
            seconds=self.settings.human_auth_continuation_grant_ttl_seconds
        )
        token = create_human_auth_continuation_grant(
            session, human_identity_id=human_identity_id,
            source_auth_transaction_id=locked.id, now=locked_now, expires_at=expires_at,
        )
        return HumanIdentityContinued(
            human_identity_id=human_identity_id,
            continuation_grant=HumanAuthContinuationGrant(
                token=token, purpose="DEVICE_BOOTSTRAP", expires_at=expires_at,
            ),
        )

    def consume_continuation_grant(
        self, session: Session, *, token: str, expected_human_identity_id: str,
        purpose: str = "DEVICE_BOOTSTRAP", now: datetime | None = None,
    ) -> str:
        if (
            not isinstance(token, str) or not token.startswith("hcg_")
            or len(token) > 256 or not token.isascii()
        ):
            raise HumanAuthContinuationGrantRejected()
        digest = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
        row = consume_human_auth_continuation_grant(
            session, token_digest=digest,
            expected_human_identity_id=expected_human_identity_id,
            purpose=purpose, now=now if now is not None else datetime.now(UTC),
        )
        if row is None:
            raise HumanAuthContinuationGrantRejected()
        return row.human_identity_id

    def revoke_continuation_grant(
        self, session: Session, *, token: str, expected_human_identity_id: str,
        now: datetime | None = None,
    ) -> bool:
        if not isinstance(token, str) or not token.startswith("hcg_") or not token.isascii():
            return False
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        return revoke_human_auth_continuation_grant(
            session, token_digest=digest,
            expected_human_identity_id=expected_human_identity_id,
            now=now if now is not None else datetime.now(UTC),
        )
