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
class GmailStagedAttachment:
    attachment_id: str
    artifact_id: str
    external_receipt_id: str
    content_sha256: str
    filename: str
    mime_type: str
    size_bytes: int
    storage_provider: str
    storage_reference: str


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


GmailMessagePreparer = Callable[
    [str],
    tuple[GmailMessage, tuple[GmailStagedAttachment, ...]],
]


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


def gmail_message_to_normalized_input(
    message: GmailMessage,
    *,
    staged_attachments: tuple[GmailStagedAttachment, ...] = (),
) -> dict[str, Any]:
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

    if staged_attachments:
        if len(staged_attachments) != len(message.attachments):
            raise GmailConnectorError("GMAIL_STAGED_ATTACHMENT_MISMATCH")
        normalized_attachments = []
        for summary, staged in zip(message.attachments, staged_attachments, strict=True):
            if (
                summary.attachment_id != staged.attachment_id
                or summary.mime_type != staged.mime_type
                or summary.size_bytes != staged.size_bytes
            ):
                raise GmailConnectorError("GMAIL_STAGED_ATTACHMENT_MISMATCH")
            normalized_attachments.append(
                {
                    "attachment_id": staged.attachment_id,
                    "artifact_id": staged.artifact_id,
                    "external_receipt_id": staged.external_receipt_id,
                    "content_sha256": staged.content_sha256,
                    "original_filename": staged.filename,
                    "artifact_kind": _artifact_kind(staged.mime_type),
                    "mime_type": staged.mime_type,
                    "size_bytes": staged.size_bytes,
                    "storage_provider": staged.storage_provider,
                    "storage_reference": staged.storage_reference,
                }
            )
    else:
        normalized_attachments = [
            {
                "attachment_id": attachment.attachment_id,
                "original_filename": attachment.filename,
                "mime_type": attachment.mime_type,
                "size_bytes": attachment.size_bytes,
            }
            for attachment in message.attachments
        ]

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
        "body_text": message.body,
        "body_observed": message.body_observed,
        "attachments_observed": message.attachments_observed,
        "message_ref": f"gmail:{message.message_id}",
        "sent_at": occurred_at,
        "attachments": normalized_attachments,
    }


def _artifact_kind(mime_type: str) -> str:
    normalized = mime_type.casefold()
    if normalized.startswith("image/"):
        return "IMAGE"
    if normalized.startswith("audio/"):
        return "AUDIO"
    if normalized.startswith("video/"):
        return "VIDEO"
    return "DOCUMENT"



class GmailInboundConnector:
    """Read Gmail, normalize one event, and submit only through neutral ingress."""

    def __init__(
        self,
        *,
        reader: GmailReader,
        ingress: IntegrationIngressClient,
        config: GmailConnectorConfig,
        message_preparer: GmailMessagePreparer | None = None,
    ):
        self._reader = reader
        self._ingress = ingress
        self._config = config
        self._message_preparer = message_preparer
        self._adapter = EmailNormalizedAdapter()

    @property
    def attachment_mode(self) -> bool:
        return self._message_preparer is not None

    def prepare_message(
        self,
        message_id: str,
    ) -> tuple[GmailMessage, tuple[GmailStagedAttachment, ...]]:
        if self._message_preparer is not None:
            return self._message_preparer(message_id)
        return self._reader.read_message(message_id), ()

    def ingest_message(
        self,
        message: GmailMessage,
        *,
        staged_attachments: tuple[GmailStagedAttachment, ...] = (),
        received_at: datetime | None = None,
    ) -> IntegrationIngressResponse:
        normalized = gmail_message_to_normalized_input(
            message,
            staged_attachments=staged_attachments,
        )
        if message.attachments and not staged_attachments:
            raise GmailConnectorError("GMAIL_ATTACHMENTS_REQUIRE_ARTIFACT_PLANE")
        if staged_attachments and not message.attachments:
            raise GmailConnectorError("GMAIL_STAGED_ATTACHMENT_MISMATCH")

        output = self._adapter.normalize(
            normalized,
            tenant_id=self._config.tenant_id,
            instance_id=self._config.instance_id,
            account_id=self._config.account_id,
            received_at=received_at or datetime.now(UTC),
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
            message, staged = self.prepare_message(message_id)
            response = self.ingest_message(
                message,
                staged_attachments=staged,
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
    "GmailStagedAttachment",
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
