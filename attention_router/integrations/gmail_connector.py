from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parseaddr
import json
from typing import Any, Callable, Protocol
from urllib import error as urllib_error
from urllib import request as urllib_request

from attention_router.integrations.channel_adapters import EmailNormalizedAdapter
from attention_router.integrations.http_transport import urlopen_without_redirects


class GmailConnectorError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GmailAttachmentSummary:
    attachment_id: str | None
    filename: str
    mime_type: str
    size_bytes: int | None


@dataclass(frozen=True, slots=True)
class GmailMessage:
    message_id: str
    thread_id: str | None
    sender: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    subject: str
    body: str
    email_ts: str
    attachments: tuple[GmailAttachmentSummary, ...] = ()
    body_observed: bool = True
    attachments_observed: bool = True


@dataclass(frozen=True, slots=True)
class GmailConnectorConfig:
    tenant_id: str
    instance_id: str
    account_id: str | None
    ingress_url: str
    ingress_bearer: str = field(repr=False)

    def __post_init__(self) -> None:
        for value, reason in (
            (self.tenant_id, "GMAIL_TENANT_REQUIRED"),
            (self.instance_id, "GMAIL_INSTANCE_REQUIRED"),
            (self.ingress_url, "GMAIL_INGRESS_URL_REQUIRED"),
            (self.ingress_bearer, "GMAIL_INGRESS_BEARER_REQUIRED"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(reason)
        if not self.ingress_url.startswith(("http://", "https://")):
            raise ValueError("GMAIL_INGRESS_URL_INVALID")


class GmailReader(Protocol):
    def search_message_ids(
        self,
        *,
        query: str,
        max_results: int,
    ) -> tuple[str, ...]: ...

    def read_message(self, message_id: str) -> GmailMessage: ...


@dataclass(frozen=True, slots=True)
class IntegrationIngressResponse:
    status_code: int
    body: dict[str, Any]


class IntegrationIngressClient:
    def __init__(
        self,
        *,
        url: str,
        bearer: str,
        timeout_seconds: float = 10.0,
        opener: Callable[..., Any] | None = None,
    ):
        self._url = url
        self._bearer = bearer
        self._timeout_seconds = timeout_seconds
        self._opener = opener or urlopen_without_redirects

    def send(self, payload: dict[str, Any]) -> IntegrationIngressResponse:
        body = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib_request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._bearer}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            response = self._opener(
                request,
                timeout=self._timeout_seconds,
            )
            status_code = int(response.status)
            response_body = response.read()
        except urllib_error.HTTPError as exc:
            status_code = int(exc.code)
            response_body = exc.read()
        except (OSError, TimeoutError) as exc:
            raise GmailConnectorError("INGRESS_UNAVAILABLE") from exc

        try:
            decoded = json.loads(response_body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GmailConnectorError("INGRESS_RESPONSE_INVALID") from exc
        if not isinstance(decoded, dict):
            raise GmailConnectorError("INGRESS_RESPONSE_INVALID")

        if status_code not in {200, 202}:
            reason = decoded.get("error_code")
            if not isinstance(reason, str) or not reason:
                reason = "INGRESS_REJECTED"
            raise GmailConnectorError(reason)
        if decoded.get("status") not in {"accepted", "duplicate"}:
            raise GmailConnectorError("INGRESS_RESPONSE_INVALID")
        return IntegrationIngressResponse(
            status_code=status_code,
            body=decoded,
        )


@dataclass(frozen=True, slots=True)
class GmailPollResult:
    selected: int = 0
    accepted: int = 0
    duplicates: int = 0


def _utc_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise GmailConnectorError("GMAIL_TIMESTAMP_REQUIRED")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise GmailConnectorError("GMAIL_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # Gmail connector surfaces can return a UTC wall clock without an
        # explicit offset. The connector contract fixes that ambiguity to UTC.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def gmail_message_to_normalized_input(message: GmailMessage) -> dict[str, Any]:
    sender_name, sender_address = parseaddr(message.sender)
    sender_address = sender_address.strip()
    if (
        not sender_address
        or "@" not in sender_address
        or sender_address.startswith("@")
        or sender_address.endswith("@")
        or any(character.isspace() for character in sender_address)
    ):
        raise GmailConnectorError("GMAIL_SENDER_INVALID")
    occurred_at = _utc_timestamp(message.email_ts)

    return {
        "message_id": message.message_id,
        "thread_id": message.thread_id or message.message_id,
        "from": {
            "address": sender_address,
            "name": sender_name.strip() or None,
        },
        "to": list(message.to),
        "cc": list(message.cc),
        "bcc": list(message.bcc),
        "subject": message.subject,
        # The body is intentionally used only to derive body_present in the
        # provider-neutral adapter. It is not serialized into the V1 event.
        "body_text": message.body,
        "body_observed": message.body_observed,
        "attachments_observed": message.attachments_observed,
        "message_ref": f"gmail:{message.message_id}",
        "sent_at": occurred_at,
        "attachments": [
            {
                "attachment_id": attachment.attachment_id,
                "original_filename": attachment.filename,
                "mime_type": attachment.mime_type,
                "size_bytes": attachment.size_bytes,
            }
            for attachment in message.attachments
        ],
    }


class GmailInboundConnector:
    """Read Gmail, normalize one event, and submit only through neutral ingress."""

    def __init__(
        self,
        *,
        reader: GmailReader,
        ingress: IntegrationIngressClient,
        config: GmailConnectorConfig,
    ):
        self._reader = reader
        self._ingress = ingress
        self._config = config
        self._adapter = EmailNormalizedAdapter()

    def ingest_message(self, message: GmailMessage) -> IntegrationIngressResponse:
        normalized = gmail_message_to_normalized_input(message)
        # Attachment bytes/receipts require the Artifact Plane upload boundary.
        # Until that exists, fail closed rather than dropping attachment identity.
        if normalized["attachments"]:
            raise GmailConnectorError("GMAIL_ATTACHMENTS_REQUIRE_ARTIFACT_PLANE")

        output = self._adapter.normalize(
            normalized,
            tenant_id=self._config.tenant_id,
            instance_id=self._config.instance_id,
            account_id=self._config.account_id,
            received_at=datetime.now(UTC),
        )
        return self._ingress.send(
            output.event.model_dump(mode="json")
        )

    def poll(
        self,
        *,
        query: str = "",
        max_results: int = 20,
    ) -> GmailPollResult:
        if type(max_results) is not int or not 1 <= max_results <= 100:
            raise ValueError("GMAIL_POLL_LIMIT_OUT_OF_RANGE")
        message_ids = self._reader.search_message_ids(
            query=query,
            max_results=max_results,
        )[:max_results]
        accepted = 0
        duplicates = 0
        for message_id in message_ids:
            response = self.ingest_message(
                self._reader.read_message(message_id)
            )
            if response.body["status"] == "accepted":
                accepted += 1
            else:
                duplicates += 1
        return GmailPollResult(
            selected=len(message_ids),
            accepted=accepted,
            duplicates=duplicates,
        )


__all__ = [
    "GmailAttachmentSummary",
    "GmailConnectorConfig",
    "GmailConnectorError",
    "GmailInboundConnector",
    "GmailMessage",
    "GmailPollResult",
    "GmailReader",
    "IntegrationIngressClient",
    "IntegrationIngressResponse",
    "gmail_message_to_normalized_input",
]
