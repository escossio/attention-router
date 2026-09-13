"""Verify transient Google assertions without creating application authority."""

from datetime import UTC, datetime

from google.auth.exceptions import TransportError
from google.auth.transport.requests import Request
from google.oauth2 import id_token as google_id_token

from attention_router.core.human_identity import (
    HumanAuthCredentialRejected,
    HumanAuthProviderUnavailable,
    VerifiedProviderIdentity,
)


class GoogleIdentityVerifier:
    def __init__(self, *, audience: str):
        self.audience = audience

    def verify(self, id_token: str) -> VerifiedProviderIdentity:
        try:
            claims = google_id_token.verify_oauth2_token(
                id_token, Request(), audience=self.audience,
            )
        except TransportError as error:
            raise HumanAuthProviderUnavailable() from error
        except ValueError as error:
            raise HumanAuthCredentialRejected() from error

        if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
            raise HumanAuthCredentialRejected()
        subject = claims.get("sub")
        nonce = claims.get("nonce")
        if not isinstance(subject, str) or not subject:
            raise HumanAuthCredentialRejected()
        if not isinstance(nonce, str) or not nonce:
            raise HumanAuthCredentialRejected()

        issued_timestamp = claims.get("iat")
        expires_timestamp = claims.get("exp")
        if type(issued_timestamp) is not int or type(expires_timestamp) is not int:
            raise HumanAuthCredentialRejected()
        try:
            issued_at = datetime.fromtimestamp(issued_timestamp, tz=UTC)
            expires_at = datetime.fromtimestamp(expires_timestamp, tz=UTC)
        except (ValueError, TypeError, OverflowError, OSError) as error:
            raise HumanAuthCredentialRejected() from error

        return VerifiedProviderIdentity(
            provider="google",
            subject=subject,
            nonce=nonce,
            issued_at=issued_at,
            expires_at=expires_at,
        )
