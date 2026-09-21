from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from typing import Callable, Protocol
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from sqlalchemy.orm import Session

from attention_router.application.gmail_connection import GMAIL_METADATA_SCOPE
from attention_router.config import Settings
from attention_router.infrastructure.models import (
    IntegrationBindingRow,
    IntegrationCredentialRow,
)
from attention_router.infrastructure.provider_authorization_models import (
    ProviderAuthorizationRow,
)
from attention_router.integrations.gmail_api_reader import (
    GmailApiReader,
    StaticGmailAccessTokenProvider,
)
from attention_router.integrations.gmail_connector import (
    GmailConnectorConfig,
    GmailInboundConnector,
    GmailPollResult,
    GmailReader,
    IntegrationIngressClient,
)
from attention_router.integrations.tenant_binding import INBOUND_SCOPE, credential_digest
from attention_router.security.provider_secrets import (
    ProviderSecretCipher,
    ProviderSecretEnvelope,
)


class GmailProductRunnerError(RuntimeError):
    code = "GMAIL_PRODUCT_RUNNER_UNAVAILABLE"


class GmailProductRunnerDisabled(GmailProductRunnerError):
    code = "GMAIL_PRODUCT_RUNNER_DISABLED"


class GmailProductAuthorizationUnavailable(GmailProductRunnerError):
    code = "GMAIL_PRODUCT_AUTHORIZATION_UNAVAILABLE"


class GmailProductAuthorizationInvalid(GmailProductRunnerError):
    code = "GMAIL_PRODUCT_AUTHORIZATION_INVALID"


class GoogleAccessTokenRefresher(Protocol):
    def refresh_access_token(self, refresh_token: str) -> str: ...


class GoogleRefreshAccessTokenClient:
    TOKEN_URL = "https://oauth2.googleapis.com/token"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        timeout_seconds: float = 10.0,
        opener=None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout_seconds = timeout_seconds
        self._opener = opener or urllib_request.urlopen

    def refresh_access_token(self, refresh_token: str) -> str:
        if not isinstance(refresh_token, str) or not refresh_token:
            raise GmailProductAuthorizationInvalid()
        body = urllib_parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
        ).encode("ascii")
        request = urllib_request.Request(
            self.TOKEN_URL,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            response = self._opener(request, timeout=self._timeout_seconds)
            raw = response.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise GmailProductRunnerError()
            payload = json.loads(raw.decode("utf-8"))
        except urllib_error.HTTPError as exc:
            if exc.code in {400, 401}:
                raise GmailProductAuthorizationInvalid() from None
            raise GmailProductRunnerError() from None
        except (OSError, TimeoutError, urllib_error.URLError, UnicodeError, json.JSONDecodeError):
            raise GmailProductRunnerError() from None
        if int(getattr(response, "status", 0)) != 200 or not isinstance(payload, dict):
            raise GmailProductRunnerError()
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise GmailProductRunnerError()
        raw_scope = payload.get("scope")
        if isinstance(raw_scope, str) and raw_scope.strip():
            if set(raw_scope.split()) != {GMAIL_METADATA_SCOPE}:
                raise GmailProductAuthorizationInvalid()
        return token


@dataclass(frozen=True, slots=True)
class GmailProductRunResult:
    installation_id: str
    binding_id: str
    poll: GmailPollResult


ReaderFactory = Callable[[str], GmailReader]
IngressFactory = Callable[[str], IntegrationIngressClient]


class GmailProductRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        token_refresher: GoogleAccessTokenRefresher | None = None,
        reader_factory: ReaderFactory | None = None,
        ingress_factory: IngressFactory | None = None,
    ):
        self.settings = settings
        self._token_refresher = token_refresher or GoogleRefreshAccessTokenClient(
            client_id=settings.google_workspace_oauth_client_id or "",
            client_secret=settings.google_workspace_oauth_client_secret or "",
        )
        self._reader_factory = reader_factory or (
            lambda token: GmailApiReader(
                token_provider=StaticGmailAccessTokenProvider(token)
            )
        )
        self._ingress_factory = ingress_factory or (
            lambda bearer: IntegrationIngressClient(
                url=settings.gmail_product_runner_ingress_url,
                bearer=bearer,
            )
        )

    @staticmethod
    def _aad(row: ProviderAuthorizationRow) -> bytes:
        return (
            f"{row.id}|{row.tenant_id}|{row.human_identity_id}|GOOGLE|GMAIL"
        ).encode("utf-8")

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _load_governed_authorization(
        self,
        session: Session,
        installation_id: str,
        *,
        now: datetime,
    ) -> tuple[ProviderAuthorizationRow, IntegrationBindingRow, str, str]:
        row = session.get(ProviderAuthorizationRow, installation_id)
        if row is None or row.status != "ACTIVE":
            raise GmailProductAuthorizationUnavailable()
        if (
            row.provider != "GOOGLE"
            or row.product != "GMAIL"
            or set(row.granted_scopes) != {GMAIL_METADATA_SCOPE}
        ):
            raise GmailProductAuthorizationInvalid()

        binding = session.get(IntegrationBindingRow, row.integration_binding_id)
        credential = session.get(
            IntegrationCredentialRow,
            row.integration_credential_id,
        )
        if (
            binding is None
            or not binding.active
            or binding.tenant_id != row.tenant_id
            or binding.audience != self.settings.integration_ingress_audience
            or binding.name != "channel.email"
            or binding.kind != "CHANNEL"
            or binding.account_key != "sha256:" + row.provider_account_hash
            or set(binding.scopes) != {INBOUND_SCOPE}
            or credential is None
            or credential.revoked
            or credential.binding_id != binding.id
            or set(credential.scopes) != {INBOUND_SCOPE}
            or now < self._utc(credential.not_before)
            or now >= self._utc(credential.expires_at)
        ):
            raise GmailProductAuthorizationInvalid()

        key = self.settings.provider_authorization_key_b64url
        if not key:
            raise GmailProductRunnerDisabled()
        try:
            refresh_token, ingress_bearer = ProviderSecretCipher(key).decrypt(
                ProviderSecretEnvelope(
                    row.secret_nonce_b64url,
                    row.secret_ciphertext_b64url,
                    row.secret_key_version,
                ),
                aad=self._aad(row),
            )
        except Exception as exc:
            raise GmailProductAuthorizationInvalid() from exc
        if credential_digest(ingress_bearer) != credential.digest:
            raise GmailProductAuthorizationInvalid()
        return row, binding, refresh_token, ingress_bearer

    def run_once(
        self,
        session: Session,
        *,
        installation_id: str,
        max_results: int | None = None,
        now: datetime | None = None,
    ) -> GmailProductRunResult:
        if not self.settings.gmail_product_runner_enabled:
            raise GmailProductRunnerDisabled()
        if not isinstance(installation_id, str) or not installation_id:
            raise GmailProductAuthorizationUnavailable()
        limit = (
            self.settings.gmail_product_runner_max_results
            if max_results is None
            else max_results
        )
        if not 1 <= limit <= 100:
            raise ValueError("GMAIL_POLL_LIMIT_OUT_OF_RANGE")

        current = (now or datetime.now(UTC)).astimezone(UTC)
        row, binding, refresh_token, ingress_bearer = self._load_governed_authorization(
            session,
            installation_id,
            now=current,
        )
        access_token = self._token_refresher.refresh_access_token(refresh_token)

        reader = self._reader_factory(access_token)
        ingress = self._ingress_factory(ingress_bearer)
        connector = GmailInboundConnector(
            reader=reader,
            ingress=ingress,
            config=GmailConnectorConfig(
                tenant_id=row.tenant_id,
                instance_id=binding.instance_id,
                account_id=binding.account_key or None,
                ingress_url=self.settings.gmail_product_runner_ingress_url,
                ingress_bearer=ingress_bearer,
            ),
        )
        poll = connector.poll(max_results=limit)
        return GmailProductRunResult(
            installation_id=row.id,
            binding_id=binding.id,
            poll=poll,
        )


__all__ = [
    "GmailProductAuthorizationInvalid",
    "GmailProductAuthorizationUnavailable",
    "GmailProductRunResult",
    "GmailProductRunner",
    "GmailProductRunnerDisabled",
    "GmailProductRunnerError",
    "GoogleAccessTokenRefresher",
    "GoogleRefreshAccessTokenClient",
]
