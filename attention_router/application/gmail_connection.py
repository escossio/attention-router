from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import secrets
from typing import Protocol
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.client_session import (
    ClientSessionService,
)
from attention_router.config import Settings
from attention_router.infrastructure.gmail_slot_lock import acquire_gmail_slot
from attention_router.infrastructure.models import (
    IntegrationBindingRow,
    IntegrationCredentialRow,
    TenantRow,
)
from attention_router.infrastructure.provider_authorization_models import (
    ProviderAuthorizationRow,
)
from attention_router.integrations.tenant_binding import (
    INBOUND_SCOPE,
    credential_digest,
)
from attention_router.security.provider_secrets import (
    ProviderSecretCipher,
    ProviderSecretEnvelope,
)


GMAIL_METADATA_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
logger = logging.getLogger("uvicorn.error")

# Provider text is untrusted: only these exact, public error identifiers may
# enter diagnostics. Never log messages, ErrorInfo metadata or response bodies.
_GMAIL_ERROR_REASONS = frozenset({
    "accessNotConfigured", "SERVICE_DISABLED", "SERVICE_BLOCKED",
    "insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
    "authError", "CREDENTIALS_MISSING", "ACCESS_TOKEN_EXPIRED",
    "ACCESS_TOKEN_INVALID", "domainPolicy", "DOMAIN_POLICY",
    "rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded",
    "quotaExceeded", "RATE_LIMIT_EXCEEDED", "QUOTA_EXCEEDED",
    "backendError", "forbidden", "BILLING_DISABLED", "CONSUMER_INVALID",
})
_GMAIL_ERROR_STATUSES = frozenset({
    "CANCELLED", "UNKNOWN", "INVALID_ARGUMENT", "DEADLINE_EXCEEDED",
    "NOT_FOUND", "ALREADY_EXISTS", "PERMISSION_DENIED", "RESOURCE_EXHAUSTED",
    "FAILED_PRECONDITION", "ABORTED", "OUT_OF_RANGE", "UNIMPLEMENTED",
    "INTERNAL", "UNAVAILABLE", "DATA_LOSS", "UNAUTHENTICATED",
})


class GmailConnectionError(RuntimeError):
    code = "GMAIL_CONNECTION_UNAVAILABLE"


class GmailConnectionDisabled(GmailConnectionError):
    code = "GMAIL_CONNECTION_DISABLED"


class GmailAuthorizationRejected(GmailConnectionError):
    code = "GMAIL_AUTHORIZATION_REJECTED"


class GmailRefreshTokenRequired(GmailConnectionError):
    code = "GMAIL_REFRESH_TOKEN_REQUIRED"


class GmailProviderUnavailable(GmailConnectionError):
    code = "GMAIL_PROVIDER_UNAVAILABLE"


class GmailConnectionConflict(GmailConnectionError):
    code = "GMAIL_CONNECTION_CONFLICT"


@dataclass(frozen=True, slots=True)
class GoogleTokenGrant:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    granted_scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GoogleGmailProfile:
    email_address: str


class GoogleWorkspaceOAuth(Protocol):
    def exchange_authorization_code(self, code: str) -> GoogleTokenGrant: ...
    def gmail_profile(self, access_token: str) -> GoogleGmailProfile: ...
    def revoke(self, refresh_token: str) -> None: ...


class GoogleWorkspaceOAuthClient:
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
    REVOKE_URL = "https://oauth2.googleapis.com/revoke"

    def __init__(self, *, client_id: str, client_secret: str, timeout: float = 10.0):
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout

    @staticmethod
    def _json_response(response, *, max_bytes: int = 64 * 1024) -> dict:
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise GmailProviderUnavailable()
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GmailProviderUnavailable() from exc
        if not isinstance(value, dict):
            raise GmailProviderUnavailable()
        return value

    @staticmethod
    def _profile_error_metadata(response) -> tuple[str, str]:
        unknown = ("UNKNOWN", "UNKNOWN")
        try:
            # A failed diagnostic read must not replace the original HTTP error.
            body = response.read(16 * 1024)
            if len(body) >= 16 * 1024:
                return unknown
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return unknown
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            return unknown
        raw_status = error.get("status")
        google_status = (
            raw_status
            if isinstance(raw_status, str) and raw_status in _GMAIL_ERROR_STATUSES
            else "UNKNOWN"
        )
        raw_reasons = []
        errors = error.get("errors")
        if isinstance(errors, list):
            raw_reasons.extend(item.get("reason") for item in errors if isinstance(item, dict))
        details = error.get("details")
        if isinstance(details, list):
            raw_reasons.extend(
                item.get("reason")
                for item in details
                if isinstance(item, dict)
                and item.get("@type") == "type.googleapis.com/google.rpc.ErrorInfo"
            )
        reasons = {
            value if isinstance(value, str) and value in _GMAIL_ERROR_REASONS else "UNKNOWN"
            for value in raw_reasons
        }
        return ",".join(sorted(reasons)) or "UNKNOWN", google_status

    def exchange_authorization_code(self, code: str) -> GoogleTokenGrant:
        body = urllib_parse.urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": "",
            }
        ).encode("ascii")
        request = urllib_request.Request(
            self.TOKEN_URL,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib_request.urlopen(request, timeout=self.timeout) as response:
                payload = self._json_response(response)
        except urllib_error.HTTPError as exc:
            if exc.code in {400, 401}:
                raise GmailAuthorizationRejected() from None
            raise GmailProviderUnavailable() from None
        except (OSError, TimeoutError, urllib_error.URLError):
            raise GmailProviderUnavailable() from None

        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        scope = payload.get("scope")
        if not isinstance(access_token, str) or not access_token:
            raise GmailProviderUnavailable()
        scopes = tuple(
            sorted(
                item
                for item in (scope.split() if isinstance(scope, str) else [])
                if item
            )
        )
        if GMAIL_METADATA_SCOPE not in scopes:
            raise GmailAuthorizationRejected()
        if refresh_token is not None and (
            not isinstance(refresh_token, str) or not refresh_token
        ):
            raise GmailProviderUnavailable()
        return GoogleTokenGrant(access_token, refresh_token, scopes)

    def gmail_profile(self, access_token: str) -> GoogleGmailProfile:
        request = urllib_request.Request(
            self.PROFILE_URL,
            method="GET",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib_request.urlopen(request, timeout=self.timeout) as response:
                payload = self._json_response(response)
        except urllib_error.HTTPError as exc:
            reason, google_status = self._profile_error_metadata(exc)
            http_status = exc.code if type(exc.code) is int and 100 <= exc.code <= 599 else 0
            logger.warning(
                "GMAIL_PROFILE_FAILED http_status=%d google_status=%s reason=%s",
                http_status,
                google_status,
                reason,
                extra={
                    "event": "GMAIL_PROFILE_FAILED",
                    "http_status": http_status,
                    "google_status": google_status,
                    "reason": reason,
                },
            )
            if exc.code == 403 and "SERVICE_DISABLED" in reason.split(","):
                raise GmailProviderUnavailable() from None
            if exc.code in {401, 403}:
                raise GmailAuthorizationRejected() from None
            raise GmailProviderUnavailable() from None
        except (OSError, TimeoutError, urllib_error.URLError):
            raise GmailProviderUnavailable() from None
        email = payload.get("emailAddress")
        if (
            not isinstance(email, str)
            or "@" not in email
            or len(email) > 320
        ):
            raise GmailProviderUnavailable()
        return GoogleGmailProfile(email.strip().casefold())

    def revoke(self, refresh_token: str) -> None:
        body = urllib_parse.urlencode({"token": refresh_token}).encode("ascii")
        request = urllib_request.Request(
            self.REVOKE_URL,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib_request.urlopen(request, timeout=self.timeout) as response:
                if int(response.status) != 200:
                    raise GmailProviderUnavailable()
        except urllib_error.HTTPError as exc:
            if exc.code == 400:
                return
            raise GmailProviderUnavailable() from None
        except (OSError, TimeoutError, urllib_error.URLError):
            raise GmailProviderUnavailable() from None


@dataclass(frozen=True, slots=True)
class GmailConnectionView:
    status: str
    installation_id: str | None
    granted_scopes: tuple[str, ...]


class GmailConnectionService:
    def __init__(
        self,
        *,
        settings: Settings,
        client_sessions: ClientSessionService,
        oauth: GoogleWorkspaceOAuth | None = None,
    ):
        self.settings = settings
        self.client_sessions = client_sessions
        self.oauth = oauth or GoogleWorkspaceOAuthClient(
            client_id=settings.google_workspace_oauth_client_id or "",
            client_secret=settings.google_workspace_oauth_client_secret or "",
        )

    def _require_enabled(self) -> None:
        if not self.settings.gmail_connect_enabled:
            raise GmailConnectionDisabled()

    def _cipher(self) -> ProviderSecretCipher:
        key = self.settings.provider_authorization_key_b64url
        if not key:
            raise GmailConnectionDisabled()
        return ProviderSecretCipher(key)

    @staticmethod
    def _slot_key(tenant_id: str, human_identity_id: str) -> str:
        return hashlib.sha256(
            f"{tenant_id}|{human_identity_id}|GOOGLE|GMAIL".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _aad(row_id: str, tenant_id: str, human_identity_id: str) -> bytes:
        return (
            f"{row_id}|{tenant_id}|{human_identity_id}|GOOGLE|GMAIL"
        ).encode("utf-8")

    @staticmethod
    def _lock_slot(session: Session, slot: str) -> None:
        try:
            acquire_gmail_slot(session, slot)
        except Exception:
            pass
        else:
            return
        # Raise outside the handler: no DB diagnostic context escapes.
        raise GmailConnectionConflict()

    def _authority(self, session: Session, session_token: str | None):
        return self.client_sessions.authenticated_bootstrap(
            session,
            session_token=session_token,
        )

    def status(
        self,
        session: Session,
        *,
        session_token: str | None,
    ) -> GmailConnectionView:
        self._require_enabled()
        authority = self._authority(session, session_token)
        slot = self._slot_key(
            authority.active_tenant_id,
            authority.human_identity_id,
        )
        row = session.scalar(
            select(ProviderAuthorizationRow).where(
                ProviderAuthorizationRow.slot_key == slot
            )
        )
        if row is None or row.status != "ACTIVE":
            return GmailConnectionView("DISCONNECTED", None, ())
        return GmailConnectionView(
            "CONNECTED",
            row.id,
            tuple(sorted(row.granted_scopes)),
        )

    def connect(
        self,
        session: Session,
        *,
        session_token: str | None,
        authorization_code: str,
        now: datetime | None = None,
    ) -> GmailConnectionView:
        self._require_enabled()
        if (
            not isinstance(authorization_code, str)
            or not 8 <= len(authorization_code) <= 8192
            or authorization_code.isspace()
        ):
            raise GmailAuthorizationRejected()

        authority = self._authority(session, session_token)
        grant = self.oauth.exchange_authorization_code(authorization_code)
        if set(grant.granted_scopes) != {GMAIL_METADATA_SCOPE}:
            raise GmailAuthorizationRejected()
        profile = self.oauth.gmail_profile(grant.access_token)
        current = now or datetime.now(UTC)
        account_hash = hashlib.sha256(
            profile.email_address.encode("utf-8")
        ).hexdigest()
        slot = self._slot_key(
            authority.active_tenant_id,
            authority.human_identity_id,
        )

        self._lock_slot(session, slot)

        # Revalidate client-session authority after provider network calls and slot wait.
        authority = self._authority(session, session_token)
        if slot != self._slot_key(
            authority.active_tenant_id,
            authority.human_identity_id,
        ):
            raise GmailConnectionConflict()

        tenant = session.scalar(
            select(TenantRow)
            .where(TenantRow.id == authority.active_tenant_id)
            .with_for_update()
        )
        if tenant is None or tenant.status != "ACTIVE":
            raise GmailConnectionConflict()

        row = session.scalar(
            select(ProviderAuthorizationRow)
            .where(ProviderAuthorizationRow.slot_key == slot)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        existing_refresh: str | None = None
        if row is not None:
            try:
                existing_refresh, _existing_bearer = self._cipher().decrypt(
                    ProviderSecretEnvelope(
                        row.secret_nonce_b64url,
                        row.secret_ciphertext_b64url,
                        row.secret_key_version,
                    ),
                    aad=self._aad(
                        row.id,
                        row.tenant_id,
                        row.human_identity_id,
                    ),
                )
            except Exception as exc:
                raise GmailConnectionConflict() from exc

        refresh_token = grant.refresh_token or (
            existing_refresh
            if row is not None
            and row.provider_account_hash == account_hash
            else None
        )
        if refresh_token is None:
            raise GmailRefreshTokenRequired()

        binding_id = "gmail-bind-" + hashlib.sha256(
            f"{slot}|{account_hash}".encode("ascii")
        ).hexdigest()[:32]
        account_key = "sha256:" + account_hash
        binding = session.get(IntegrationBindingRow, binding_id)
        if binding is None:
            binding = IntegrationBindingRow(
                id=binding_id,
                audience=self.settings.integration_ingress_audience,
                tenant_id=authority.active_tenant_id,
                kind="CHANNEL",
                name="channel.email",
                instance_id="gmail-" + slot[:24],
                account_key=account_key,
                active=True,
                scopes=[INBOUND_SCOPE],
            )
            session.add(binding)
            session.flush()
        else:
            if (
                binding.tenant_id != authority.active_tenant_id
                or binding.name != "channel.email"
                or binding.account_key != account_key
            ):
                raise GmailConnectionConflict()
            binding.active = True
            binding.scopes = [INBOUND_SCOPE]

        if row is not None and row.integration_binding_id != binding_id:
            old_binding = session.get(
                IntegrationBindingRow,
                row.integration_binding_id,
            )
            if old_binding is not None:
                old_binding.active = False
        if row is not None:
            old_credential = session.get(
                IntegrationCredentialRow,
                row.integration_credential_id,
            )
            if old_credential is not None:
                old_credential.revoked = True

        integration_bearer = secrets.token_urlsafe(32)
        credential_id = "gmail-cred-" + uuid4().hex
        credential = IntegrationCredentialRow(
            id=credential_id,
            digest=credential_digest(integration_bearer),
            binding_id=binding_id,
            not_before=current - timedelta(minutes=1),
            expires_at=current + timedelta(days=180),
            revoked=False,
            scopes=[INBOUND_SCOPE],
        )
        session.add(credential)
        session.flush()

        row_id = row.id if row is not None else "paz_" + uuid4().hex
        envelope = self._cipher().encrypt(
            refresh_token=refresh_token,
            integration_bearer=integration_bearer,
            aad=self._aad(
                row_id,
                authority.active_tenant_id,
                authority.human_identity_id,
            ),
        )
        if row is None:
            row = ProviderAuthorizationRow(
                id=row_id,
                slot_key=slot,
                tenant_id=authority.active_tenant_id,
                human_identity_id=authority.human_identity_id,
                provider="GOOGLE",
                product="GMAIL",
                provider_account_hash=account_hash,
                granted_scopes=list(grant.granted_scopes),
                secret_nonce_b64url=envelope.nonce_b64url,
                secret_ciphertext_b64url=envelope.ciphertext_b64url,
                secret_key_version=envelope.key_version,
                integration_binding_id=binding_id,
                integration_credential_id=credential_id,
                status="ACTIVE",
                created_at=current,
                updated_at=current,
                revoked_at=None,
            )
            session.add(row)
        else:
            if row.provider_account_hash != account_hash:
                row.gmail_history_id = None
            row.provider_account_hash = account_hash
            row.granted_scopes = list(grant.granted_scopes)
            row.secret_nonce_b64url = envelope.nonce_b64url
            row.secret_ciphertext_b64url = envelope.ciphertext_b64url
            row.secret_key_version = envelope.key_version
            row.integration_binding_id = binding_id
            row.integration_credential_id = credential_id
            row.status = "ACTIVE"
            row.updated_at = current
            row.revoked_at = None
        session.flush()
        return GmailConnectionView(
            "CONNECTED",
            row.id,
            tuple(sorted(row.granted_scopes)),
        )

    def disconnect(
        self,
        session: Session,
        *,
        session_token: str | None,
        now: datetime | None = None,
    ) -> GmailConnectionView:
        self._require_enabled()
        authority = self._authority(session, session_token)
        slot = self._slot_key(
            authority.active_tenant_id,
            authority.human_identity_id,
        )
        self._lock_slot(session, slot)
        row = session.scalar(
            select(ProviderAuthorizationRow)
            .where(ProviderAuthorizationRow.slot_key == slot)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None or row.status != "ACTIVE":
            return GmailConnectionView("DISCONNECTED", None, ())

        try:
            refresh_token, _integration_bearer = self._cipher().decrypt(
                ProviderSecretEnvelope(
                    row.secret_nonce_b64url,
                    row.secret_ciphertext_b64url,
                    row.secret_key_version,
                ),
                aad=self._aad(
                    row.id,
                    row.tenant_id,
                    row.human_identity_id,
                ),
            )
        except Exception as exc:
            raise GmailConnectionConflict() from exc
        self.oauth.revoke(refresh_token)

        binding = session.get(
            IntegrationBindingRow,
            row.integration_binding_id,
        )
        if binding is not None:
            binding.active = False
        credential = session.get(
            IntegrationCredentialRow,
            row.integration_credential_id,
        )
        if credential is not None:
            credential.revoked = True

        current = now or datetime.now(UTC)
        row.status = "REVOKED"
        row.updated_at = current
        row.revoked_at = current
        session.flush()
        return GmailConnectionView("DISCONNECTED", None, ())
