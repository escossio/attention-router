from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr
import hashlib
import json
import logging
import os
from pathlib import Path
import stat
import time
from typing import Callable, Protocol
from urllib.parse import quote

import httpx

from attention_router.integrations.channel_adapters import EmailNormalizedAdapter
from attention_router.integrations.http_client import (
    IntegrationIngressClientError,
    NeutralIntegrationIngressClient,
    serialize_inbound_event,
)


logger = logging.getLogger("attention-router-gmail-connector")

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_REQUIRED_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"


class GmailConnectorError(RuntimeError):
    pass


class GmailAuthenticationError(GmailConnectorError):
    pass


class GmailProviderUnavailable(GmailConnectorError):
    pass


class GmailHistoryExpired(GmailConnectorError):
    pass


class GmailBacklogLimitExceeded(GmailConnectorError):
    pass


class GmailPayloadError(GmailConnectorError):
    pass


@dataclass(frozen=True, slots=True)
class GmailCursorState:
    mailbox_sha256: str
    history_id: str
    schema_version: int = 1


class GmailCursorStore(Protocol):
    def load(self) -> GmailCursorState | None: ...
    def save(self, state: GmailCursorState) -> None: ...


def _read_private_secret_file(path_value: str, *, error_prefix: str) -> str:
    path = Path(path_value)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise GmailConnectorError(f"{error_prefix}_UNAVAILABLE") from exc
    if mode & 0o077:
        raise GmailConnectorError(f"{error_prefix}_PERMISSIONS_UNSAFE")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise GmailConnectorError(f"{error_prefix}_UNAVAILABLE") from exc
    if not value:
        raise GmailConnectorError(f"{error_prefix}_REQUIRED")
    return value


class FileGmailCursorStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> GmailCursorState | None:
        if not self.path.exists():
            return None
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            raise GmailConnectorError("GMAIL_CURSOR_STATE_PERMISSIONS_UNSAFE")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise GmailConnectorError("GMAIL_CURSOR_STATE_INVALID") from exc
        if not isinstance(payload, dict):
            raise GmailConnectorError("GMAIL_CURSOR_STATE_INVALID")
        mailbox_sha256 = payload.get("mailbox_sha256")
        history_id = payload.get("history_id")
        if (
            payload.get("schema_version") != 1
            or not isinstance(mailbox_sha256, str)
            or len(mailbox_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in mailbox_sha256)
            or not isinstance(history_id, str)
            or not history_id.isdigit()
        ):
            raise GmailConnectorError("GMAIL_CURSOR_STATE_INVALID")
        return GmailCursorState(
            mailbox_sha256=mailbox_sha256,
            history_id=history_id,
        )

    def save(self, state: GmailCursorState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "schema_version": state.schema_version,
                "mailbox_sha256": state.mailbox_sha256,
                "history_id": state.history_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        temp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            os.chmod(self.path, 0o600)
        except Exception:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise


@dataclass(frozen=True, slots=True)
class GmailConnectorConfig:
    tenant_id: str
    instance_id: str
    account_id: str
    integration_endpoint: str
    integration_credential: str
    cursor_path: Path
    poll_interval_seconds: float = 30.0
    message_limit_per_poll: int = 100
    history_page_limit: int = 20
    request_timeout_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> "GmailConnectorConfig":
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise GmailConnectorError(f"{name}_REQUIRED")
            return value

        poll_interval = float(
            os.environ.get("GMAIL_POLL_INTERVAL_SECONDS", "30")
        )
        message_limit = int(
            os.environ.get("GMAIL_MESSAGE_LIMIT_PER_POLL", "100")
        )
        page_limit = int(
            os.environ.get("GMAIL_HISTORY_PAGE_LIMIT", "20")
        )
        timeout = float(
            os.environ.get("GMAIL_REQUEST_TIMEOUT_SECONDS", "15")
        )
        if poll_interval < 5 or poll_interval > 3600:
            raise GmailConnectorError("GMAIL_POLL_INTERVAL_OUT_OF_RANGE")
        if message_limit < 1 or message_limit > 500:
            raise GmailConnectorError("GMAIL_MESSAGE_LIMIT_OUT_OF_RANGE")
        if page_limit < 1 or page_limit > 100:
            raise GmailConnectorError("GMAIL_HISTORY_PAGE_LIMIT_OUT_OF_RANGE")
        if timeout <= 0 or timeout > 120:
            raise GmailConnectorError("GMAIL_REQUEST_TIMEOUT_OUT_OF_RANGE")

        integration_credential_file = os.environ.get(
            "ATTENTION_ROUTER_INTEGRATION_CREDENTIAL_FILE",
            "",
        ).strip()
        if integration_credential_file:
            integration_credential = _read_private_secret_file(
                integration_credential_file,
                error_prefix="ATTENTION_ROUTER_INTEGRATION_CREDENTIAL_FILE",
            )
        else:
            integration_credential = required(
                "ATTENTION_ROUTER_INTEGRATION_CREDENTIAL"
            )

        return cls(
            tenant_id=required("GMAIL_TENANT_ID"),
            instance_id=required("GMAIL_INTEGRATION_INSTANCE_ID"),
            account_id=required("GMAIL_INTEGRATION_ACCOUNT_ID"),
            integration_endpoint=required(
                "ATTENTION_ROUTER_INTEGRATION_ENDPOINT"
            ),
            integration_credential=integration_credential,
            cursor_path=Path(required("GMAIL_CONNECTOR_STATE_PATH")),
            poll_interval_seconds=poll_interval,
            message_limit_per_poll=message_limit,
            history_page_limit=page_limit,
            request_timeout_seconds=timeout,
        )


@dataclass(frozen=True, slots=True)
class GmailPollResult:
    status: str
    messages_seen: int = 0
    accepted: int = 0
    duplicates: int = 0
    history_id: str | None = None


class GmailApiClient:
    def __init__(
        self,
        *,
        access_token_provider: Callable[[], str],
        timeout_seconds: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError("GMAIL_REQUEST_TIMEOUT_OUT_OF_RANGE")
        self._access_token_provider = access_token_provider
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "GmailApiClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: list[tuple[str, str]] | None = None,
        history_request: bool = False,
    ) -> dict:
        token = self._access_token_provider().strip()
        if not token:
            raise GmailAuthenticationError("GMAIL_ACCESS_TOKEN_UNAVAILABLE")
        try:
            response = self._client.request(
                method,
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise GmailProviderUnavailable("GMAIL_REQUEST_FAILED") from exc

        if 300 <= response.status_code < 400:
            raise GmailProviderUnavailable("GMAIL_REDIRECT_REJECTED")
        if response.status_code in {401, 403}:
            raise GmailAuthenticationError("GMAIL_AUTHENTICATION_REJECTED")
        if response.status_code == 404 and history_request:
            raise GmailHistoryExpired("GMAIL_HISTORY_CURSOR_EXPIRED")
        if response.status_code == 429 or response.status_code >= 500:
            raise GmailProviderUnavailable("GMAIL_PROVIDER_UNAVAILABLE")
        if response.status_code != 200:
            raise GmailProviderUnavailable("GMAIL_PROVIDER_RESPONSE_REJECTED")
        try:
            payload = response.json()
        except ValueError as exc:
            raise GmailProviderUnavailable("GMAIL_PROVIDER_RESPONSE_INVALID") from exc
        if not isinstance(payload, dict):
            raise GmailProviderUnavailable("GMAIL_PROVIDER_RESPONSE_INVALID")
        return payload

    def get_profile(self) -> dict:
        return self._request("GET", f"{GMAIL_API_BASE}/profile")

    def list_history_page(
        self,
        *,
        start_history_id: str,
        page_token: str | None = None,
    ) -> dict:
        params = [
            ("startHistoryId", start_history_id),
            ("historyTypes", "messageAdded"),
            ("labelId", "INBOX"),
            ("maxResults", "100"),
        ]
        if page_token is not None:
            params.append(("pageToken", page_token))
        return self._request(
            "GET",
            f"{GMAIL_API_BASE}/history",
            params=params,
            history_request=True,
        )

    def get_message_metadata(self, message_id: str) -> dict:
        if not message_id:
            raise GmailPayloadError("GMAIL_MESSAGE_ID_INVALID")
        encoded_message_id = quote(message_id, safe="")
        return self._request(
            "GET",
            f"{GMAIL_API_BASE}/messages/{encoded_message_id}",
            params=[
                ("format", "metadata"),
                ("metadataHeaders", "From"),
            ],
        )


def _mailbox_fingerprint(email_address: str) -> str:
    normalized = email_address.strip().casefold()
    if not normalized or "@" not in normalized:
        raise GmailPayloadError("GMAIL_PROFILE_EMAIL_INVALID")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _profile_state(profile: dict) -> GmailCursorState:
    email_address = profile.get("emailAddress")
    history_id = profile.get("historyId")
    if (
        not isinstance(email_address, str)
        or not isinstance(history_id, str)
        or not history_id.isdigit()
    ):
        raise GmailPayloadError("GMAIL_PROFILE_INVALID")
    return GmailCursorState(
        mailbox_sha256=_mailbox_fingerprint(email_address),
        history_id=history_id,
    )


def _metadata_header(message: dict, name: str) -> str | None:
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return None
    headers = payload.get("headers")
    if not isinstance(headers, list):
        return None
    target = name.casefold()
    for header in headers:
        if not isinstance(header, dict):
            continue
        header_name = header.get("name")
        value = header.get("value")
        if (
            isinstance(header_name, str)
            and header_name.casefold() == target
            and isinstance(value, str)
        ):
            return value
    return None


def _gmail_message_to_normalized_input(message: dict) -> dict:
    message_id = message.get("id")
    thread_id = message.get("threadId")
    internal_date = message.get("internalDate")
    if (
        not isinstance(message_id, str)
        or not message_id
        or not isinstance(thread_id, str)
        or not thread_id
        or not isinstance(internal_date, str)
        or not internal_date.isdigit()
    ):
        raise GmailPayloadError("GMAIL_MESSAGE_METADATA_INVALID")

    raw_from = _metadata_header(message, "From")
    if raw_from is None:
        raise GmailPayloadError("GMAIL_MESSAGE_FROM_MISSING")
    display_name, address = parseaddr(raw_from)
    address = address.strip().casefold()
    if not address or "@" not in address:
        raise GmailPayloadError("GMAIL_MESSAGE_FROM_INVALID")

    sent_at = datetime.fromtimestamp(
        int(internal_date) / 1000,
        tz=UTC,
    )
    return {
        "message_id": message_id,
        "thread_id": thread_id,
        "from": {
            "address": address,
            "name": display_name.strip() or None,
        },
        "sent_at": sent_at,
        "message_ref": f"gmail:{message_id}",
        "attachments": [],
    }


class GmailPollingConnector:
    def __init__(
        self,
        *,
        config: GmailConnectorConfig,
        gmail: GmailApiClient,
        ingress: NeutralIntegrationIngressClient,
        cursor_store: GmailCursorStore,
        adapter: EmailNormalizedAdapter | None = None,
    ) -> None:
        self.config = config
        self.gmail = gmail
        self.ingress = ingress
        self.cursor_store = cursor_store
        self.adapter = adapter or EmailNormalizedAdapter()

    def poll_once(self) -> GmailPollResult:
        profile = _profile_state(self.gmail.get_profile())
        state = self.cursor_store.load()

        if state is None:
            self.cursor_store.save(profile)
            return GmailPollResult(
                status="BOOTSTRAPPED",
                history_id=profile.history_id,
            )
        if state.mailbox_sha256 != profile.mailbox_sha256:
            raise GmailConnectorError("GMAIL_MAILBOX_IDENTITY_CHANGED")
        if state.history_id == profile.history_id:
            return GmailPollResult(
                status="IDLE",
                history_id=state.history_id,
            )

        message_ids: list[str] = []
        seen: set[str] = set()
        next_page: str | None = None
        final_history_id: str | None = None

        for _page_index in range(self.config.history_page_limit):
            page = self.gmail.list_history_page(
                start_history_id=state.history_id,
                page_token=next_page,
            )
            page_history_id = page.get("historyId")
            if not isinstance(page_history_id, str) or not page_history_id.isdigit():
                raise GmailPayloadError("GMAIL_HISTORY_RESPONSE_INVALID")
            final_history_id = page_history_id

            history = page.get("history") or []
            if not isinstance(history, list):
                raise GmailPayloadError("GMAIL_HISTORY_RESPONSE_INVALID")
            for record in history:
                if not isinstance(record, dict):
                    raise GmailPayloadError("GMAIL_HISTORY_RESPONSE_INVALID")
                additions = record.get("messagesAdded") or []
                if not isinstance(additions, list):
                    raise GmailPayloadError("GMAIL_HISTORY_RESPONSE_INVALID")
                for addition in additions:
                    if not isinstance(addition, dict):
                        raise GmailPayloadError("GMAIL_HISTORY_RESPONSE_INVALID")
                    message = addition.get("message")
                    if not isinstance(message, dict):
                        raise GmailPayloadError("GMAIL_HISTORY_RESPONSE_INVALID")
                    message_id = message.get("id")
                    if not isinstance(message_id, str) or not message_id:
                        raise GmailPayloadError("GMAIL_HISTORY_RESPONSE_INVALID")
                    if message_id in seen:
                        continue
                    seen.add(message_id)
                    message_ids.append(message_id)
                    if len(message_ids) > self.config.message_limit_per_poll:
                        raise GmailBacklogLimitExceeded(
                            "GMAIL_MESSAGE_BACKLOG_LIMIT_EXCEEDED"
                        )

            token = page.get("nextPageToken")
            if token is None:
                next_page = None
                break
            if not isinstance(token, str) or not token:
                raise GmailPayloadError("GMAIL_HISTORY_RESPONSE_INVALID")
            next_page = token
        else:
            raise GmailBacklogLimitExceeded(
                "GMAIL_HISTORY_PAGE_LIMIT_EXCEEDED"
            )

        if next_page is not None or final_history_id is None:
            raise GmailPayloadError("GMAIL_HISTORY_RESPONSE_INCOMPLETE")

        accepted = 0
        duplicates = 0
        for message_id in message_ids:
            metadata = self.gmail.get_message_metadata(message_id)
            raw_event = _gmail_message_to_normalized_input(metadata)
            output = self.adapter.normalize(
                raw_event,
                tenant_id=self.config.tenant_id,
                instance_id=self.config.instance_id,
                account_id=self.config.account_id,
                received_at=raw_event["sent_at"],
            )
            if output.artifact_receipts:
                raise GmailConnectorError(
                    "GMAIL_METADATA_CONNECTOR_ARTIFACTS_UNEXPECTED"
                )
            body = serialize_inbound_event(output.event)
            receipt = self.ingress.send(
                output.event,
                serialized_body=body,
            )
            if receipt.status == "accepted":
                accepted += 1
            else:
                duplicates += 1

        self.cursor_store.save(
            GmailCursorState(
                mailbox_sha256=state.mailbox_sha256,
                history_id=final_history_id,
            )
        )
        return GmailPollResult(
            status="SYNCED",
            messages_seen=len(message_ids),
            accepted=accepted,
            duplicates=duplicates,
            history_id=final_history_id,
        )


def _access_token_from_env() -> str:
    token_file = os.environ.get("GMAIL_ACCESS_TOKEN_FILE", "").strip()
    if token_file:
        try:
            return _read_private_secret_file(
                token_file,
                error_prefix="GMAIL_ACCESS_TOKEN_FILE",
            )
        except GmailConnectorError as exc:
            raise GmailAuthenticationError(str(exc)) from exc
    return os.environ.get("GMAIL_ACCESS_TOKEN", "").strip()


def run_connector(*, once: bool = False) -> None:
    config = GmailConnectorConfig.from_env()
    cursor = FileGmailCursorStore(config.cursor_path)
    with (
        GmailApiClient(
            access_token_provider=_access_token_from_env,
            timeout_seconds=config.request_timeout_seconds,
        ) as gmail,
        NeutralIntegrationIngressClient(
            endpoint=config.integration_endpoint,
            credential=config.integration_credential,
            timeout_seconds=config.request_timeout_seconds,
        ) as ingress,
    ):
        connector = GmailPollingConnector(
            config=config,
            gmail=gmail,
            ingress=ingress,
            cursor_store=cursor,
        )
        while True:
            try:
                result = connector.poll_once()
            except GmailHistoryExpired:
                logger.error("gmail sync requires explicit resynchronization")
                raise
            except (
                GmailAuthenticationError,
                GmailProviderUnavailable,
                IntegrationIngressClientError,
            ) as exc:
                logger.warning(
                    "gmail connector poll failed error_class=%s",
                    type(exc).__name__,
                )
                if once:
                    raise
            else:
                logger.info(
                    "gmail connector poll status=%s messages=%s accepted=%s duplicates=%s",
                    result.status,
                    result.messages_seen,
                    result.accepted,
                    result.duplicates,
                )
            if once:
                return
            time.sleep(config.poll_interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gmail metadata connector for Attention Router",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="perform one bootstrap/sync cycle and exit",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    run_connector(once=args.once)


__all__ = [
    "FileGmailCursorStore",
    "GMAIL_REQUIRED_SCOPE",
    "GmailApiClient",
    "GmailAuthenticationError",
    "GmailBacklogLimitExceeded",
    "GmailConnectorConfig",
    "GmailConnectorError",
    "GmailCursorState",
    "GmailHistoryExpired",
    "GmailPayloadError",
    "GmailPollResult",
    "GmailPollingConnector",
    "GmailProviderUnavailable",
    "main",
    "run_connector",
]
