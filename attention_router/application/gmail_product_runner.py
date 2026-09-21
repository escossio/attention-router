from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from typing import Callable, Protocol, TypeVar
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from sqlalchemy.orm import Session

from attention_router.application.gmail_connection import GMAIL_METADATA_SCOPE, GmailConnectionService
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
    IntegrationIngressResponse,
)
from attention_router.integrations.http_transport import urlopen_without_redirects
from attention_router.integrations.tenant_binding import INBOUND_SCOPE, credential_digest
from attention_router.security.provider_secrets import (
    ProviderSecretCipher,
    ProviderSecretEnvelope,
)


class GmailProductRunnerError(RuntimeError):
    code = "GMAIL_PRODUCT_RUNNER_UNAVAILABLE"

    def __init__(self):
        super().__init__(self.code)


class GmailProductRunnerDisabled(GmailProductRunnerError):
    code = "GMAIL_PRODUCT_RUNNER_DISABLED"


class GmailProductAuthorizationUnavailable(GmailProductRunnerError):
    code = "GMAIL_PRODUCT_AUTHORIZATION_UNAVAILABLE"


class GmailProductAuthorizationInvalid(GmailProductRunnerError):
    code = "GMAIL_PRODUCT_AUTHORIZATION_INVALID"


class GmailProductBindingInvalid(GmailProductAuthorizationInvalid):
    code = "GMAIL_PRODUCT_BINDING_INVALID"


class GmailProductSecretInvalid(GmailProductAuthorizationInvalid):
    code = "GMAIL_PRODUCT_SECRET_INVALID"


class GmailProductRefreshFailed(GmailProductRunnerError):
    code = "GMAIL_PRODUCT_REFRESH_FAILED"


class GmailProductProviderUnavailable(GmailProductRunnerError):
    code = "GMAIL_PRODUCT_PROVIDER_UNAVAILABLE"


class GmailProductIngressFailed(GmailProductRunnerError):
    code = "GMAIL_PRODUCT_INGRESS_FAILED"


T = TypeVar("T")


def _sanitized(error_type: type[GmailProductRunnerError], operation: Callable[[], T]) -> T:
    try:
        return operation()
    except Exception:
        pass
    # Raise outside the handler: neither cause nor context retains provider text.
    raise error_type()


def _exact_scope(value: object, expected: str) -> bool:
    return (
        isinstance(value, (list, tuple))
        and bool(value)
        and all(isinstance(scope, str) and scope == expected for scope in value)
    )


def _bearer_token(value: object) -> bool:
    return isinstance(value, str) and bool(value) and all(32 < ord(c) < 127 for c in value)


class GoogleAccessTokenRefresher(Protocol):
    def refresh_access_token(self, refresh_token: str) -> str: ...


class GoogleRefreshAccessTokenClient:
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    MAX_RESPONSE_BYTES = 64 * 1024

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        timeout_seconds: float = 10.0,
        opener=None,
    ):
        if type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= 30:
            raise ValueError("GMAIL_REFRESH_TIMEOUT_INVALID")
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout_seconds = timeout_seconds
        self._opener = opener or urlopen_without_redirects

    def refresh_access_token(self, refresh_token: str) -> str:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (refresh_token, self._client_id, self._client_secret)
        ):
            raise GmailProductRefreshFailed()
        payload = _sanitized(
            GmailProductRefreshFailed, lambda: self._refresh_payload(refresh_token)
        )
        if "scope" in payload:
            raw_scope = payload["scope"]
            if not isinstance(raw_scope, str) or set(raw_scope.split()) != {GMAIL_METADATA_SCOPE}:
                raise GmailProductAuthorizationInvalid()
        token = payload.get("access_token")
        token_type = payload.get("token_type")
        if (
            not _bearer_token(token)
            or not isinstance(token_type, str)
            or token_type.casefold() != "bearer"
        ):
            raise GmailProductRefreshFailed()
        if "expires_in" in payload:
            lifetime = payload["expires_in"]
            if type(lifetime) is not int or lifetime <= 0:
                raise GmailProductRefreshFailed()
        return token

    def _refresh_payload(self, refresh_token: str) -> dict:
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
        response = self._opener(request, timeout=self._timeout_seconds)
        try:
            if response.status != 200:
                raise GmailProductRefreshFailed()
            raw = response.read(self.MAX_RESPONSE_BYTES + 1)
            if len(raw) > self.MAX_RESPONSE_BYTES:
                raise GmailProductRefreshFailed()
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise GmailProductRefreshFailed()
            return payload
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()


@dataclass(frozen=True, slots=True)
class GmailProductRunResult:
    installation_id: str
    binding_id: str
    poll: GmailPollResult


ReaderFactory = Callable[[str], GmailReader]
IngressFactory = Callable[[str], IntegrationIngressClient]


class _RunnerIngress:
    """Keep ingress failures separate without changing the connector contract."""

    def __init__(self, client: IntegrationIngressClient):
        self._client = client

    def send(self, payload: dict) -> IntegrationIngressResponse:
        return _sanitized(GmailProductIngressFailed, lambda: self._client.send(payload))


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
        return GmailConnectionService._aad(row.id, row.tenant_id, row.human_identity_id)

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
        # Authority comes from persisted rows, even when the caller's Session
        # has an older identity-map entry. Polling must not autoflush writes.
        with session.no_autoflush:
            row = session.get(ProviderAuthorizationRow, installation_id, populate_existing=True)
            if row is None or row.status != "ACTIVE" or row.revoked_at is not None:
                raise GmailProductAuthorizationUnavailable()
            if (
                row.provider != "GOOGLE"
                or row.product != "GMAIL"
                or not _exact_scope(row.granted_scopes, GMAIL_METADATA_SCOPE)
            ):
                raise GmailProductAuthorizationInvalid()

            binding = session.get(
                IntegrationBindingRow, row.integration_binding_id, populate_existing=True
            )
            credential = session.get(
                IntegrationCredentialRow, row.integration_credential_id, populate_existing=True
            )
        slot = GmailConnectionService._slot_key(row.tenant_id, row.human_identity_id)
        if (
            binding is None
            or not binding.active
            or binding.tenant_id != row.tenant_id
            or binding.audience != self.settings.integration_ingress_audience
            or binding.name != "channel.email"
            or binding.kind != "CHANNEL"
            or row.slot_key != slot
            or binding.instance_id != "gmail-" + slot[:24]
            or binding.account_key != f"sha256:{row.provider_account_hash}"
            or not _exact_scope(binding.scopes, INBOUND_SCOPE)
            or credential is None
            or credential.revoked
            or credential.binding_id != binding.id
            or not _exact_scope(credential.scopes, INBOUND_SCOPE)
            or not isinstance(credential.not_before, datetime)
            or not isinstance(credential.expires_at, datetime)
            or now < self._utc(credential.not_before)
            or now >= self._utc(credential.expires_at)
        ):
            raise GmailProductBindingInvalid()

        key = self.settings.provider_authorization_key_b64url
        if not key:
            raise GmailProductRunnerDisabled()
        refresh_token, ingress_bearer = _sanitized(
            GmailProductSecretInvalid,
            lambda: ProviderSecretCipher(key).decrypt(
                ProviderSecretEnvelope(
                    row.secret_nonce_b64url,
                    row.secret_ciphertext_b64url,
                    row.secret_key_version,
                ),
                aad=self._aad(row),
            ),
        )
        if not _bearer_token(ingress_bearer):
            raise GmailProductSecretInvalid()
        digest = _sanitized(GmailProductSecretInvalid, lambda: credential_digest(ingress_bearer))
        if digest != credential.digest:
            raise GmailProductBindingInvalid()
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
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("GMAIL_POLL_LIMIT_OUT_OF_RANGE")

        current = self._utc(now or datetime.now(UTC))
        row, binding, refresh_token, ingress_bearer = self._load_governed_authorization(
            session,
            installation_id,
            now=current,
        )
        access_token = _sanitized(
            GmailProductRefreshFailed,
            lambda: self._token_refresher.refresh_access_token(refresh_token),
        )
        if not _bearer_token(access_token):
            raise GmailProductRefreshFailed()

        reader = _sanitized(
            GmailProductProviderUnavailable, lambda: self._reader_factory(access_token)
        )
        ingress = _RunnerIngress(_sanitized(
            GmailProductIngressFailed, lambda: self._ingress_factory(ingress_bearer)
        ))
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
        failure = GmailProductProviderUnavailable
        try:
            poll = connector.poll(max_results=limit)
            return GmailProductRunResult(
                installation_id=row.id,
                binding_id=binding.id,
                poll=poll,
            )
        except GmailProductIngressFailed:
            failure = GmailProductIngressFailed
        except Exception:
            pass
        raise failure()


__all__ = [
    "GmailProductAuthorizationInvalid",
    "GmailProductAuthorizationUnavailable",
    "GmailProductBindingInvalid",
    "GmailProductSecretInvalid",
    "GmailProductRefreshFailed",
    "GmailProductProviderUnavailable",
    "GmailProductIngressFailed",
    "GmailProductRunResult",
    "GmailProductRunner",
    "GmailProductRunnerDisabled",
    "GmailProductRunnerError",
    "GoogleAccessTokenRefresher",
    "GoogleRefreshAccessTokenClient",
]
