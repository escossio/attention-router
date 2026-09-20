from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
import hashlib
from typing import Iterable

from attention_router.integrations.channel_adapters import (
    AdapterNormalizationError,
    EmailNormalizedAdapter,
)
from attention_router.integrations.http_client import (
    IntegrationIngressClient,
    IntegrationIngressReceipt,
)


class EmailConnectorError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EmailMessageSnapshot:
    message_id: str
    thread_id: str
    sender_address: str
    sender_name: str | None
    sent_at: datetime
    subject_present: bool
    body_present: bool
    recipient_count: int
    message_ref: str


def _header_text(value: str | None, *, limit: int) -> str | None:
    if value is None:
        return None
    try:
        decoded = str(make_header(decode_header(value))).strip()
    except (LookupError, UnicodeError):
        decoded = value.strip()
    return decoded[:limit] if decoded else None


def _first_address(value: str | None) -> tuple[str, str | None]:
    addresses = getaddresses([value or ""])
    for display_name, address in addresses:
        normalized = address.strip().casefold()
        if normalized:
            return normalized, _header_text(display_name, limit=160)
    raise EmailConnectorError("EMAIL_SENDER_REQUIRED")


def _recipient_count(message: Message) -> int:
    values: list[str] = []
    for header in ("to", "cc"):
        values.extend(message.get_all(header, []))
    return sum(1 for _name, address in getaddresses(values) if address.strip())


def _sent_at(value: str | None) -> datetime:
    if not value:
        raise EmailConnectorError("EMAIL_DATE_REQUIRED")
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        raise EmailConnectorError("EMAIL_DATE_INVALID") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EmailConnectorError("EMAIL_DATE_TIMEZONE_REQUIRED")
    return parsed.astimezone(UTC)


def _body_present(message: Message) -> bool:
    if message.is_multipart():
        return any(
            part.get_content_maintype() == "text"
            and part.get_content_disposition() != "attachment"
            for part in message.walk()
        )
    return message.get_content_maintype() == "text"


def snapshot_rfc822_message(
    message: Message,
    *,
    message_ref: str,
    thread_id: str | None = None,
) -> EmailMessageSnapshot:
    """Extract only bounded metadata. Message bodies/attachments stay provider-side."""

    message_id = _header_text(message.get("message-id"), limit=240)
    if not message_id:
        raise EmailConnectorError("EMAIL_MESSAGE_ID_REQUIRED")
    sender_address, sender_name = _first_address(message.get("from"))
    sent_at = _sent_at(message.get("date"))
    subject_present = bool(_header_text(message.get("subject"), limit=1))
    normalized_ref = message_ref.strip()
    if not normalized_ref:
        raise EmailConnectorError("EMAIL_MESSAGE_REF_REQUIRED")
    normalized_thread = (thread_id or message_id).strip()
    if not normalized_thread:
        raise EmailConnectorError("EMAIL_THREAD_ID_REQUIRED")

    return EmailMessageSnapshot(
        message_id=message_id,
        thread_id=normalized_thread[:240],
        sender_address=sender_address[:240],
        sender_name=sender_name,
        sent_at=sent_at,
        subject_present=subject_present,
        body_present=_body_present(message),
        recipient_count=_recipient_count(message),
        message_ref=normalized_ref[:512],
    )


def snapshot_to_adapter_input(snapshot: EmailMessageSnapshot) -> dict:
    return {
        "message_id": snapshot.message_id,
        "thread_id": snapshot.thread_id,
        "from": {
            "address": snapshot.sender_address,
            "name": snapshot.sender_name,
        },
        "sent_at": snapshot.sent_at,
        "subject": "present" if snapshot.subject_present else "",
        "body_ref": snapshot.message_ref if snapshot.body_present else None,
        "to": [None] * snapshot.recipient_count,
        "message_ref": snapshot.message_ref,
        # Attachment transport belongs to Artifact Plane. Until that connector
        # boundary exists, do not pretend attachments were staged.
        "attachments": [],
    }


class EmailConnector:
    """Provider-agnostic RFC822 -> EmailNormalizedAdapter -> neutral ingress."""

    def __init__(
        self,
        *,
        tenant_id: str,
        instance_id: str,
        account_id: str | None,
        ingress: IntegrationIngressClient,
        adapter: EmailNormalizedAdapter | None = None,
    ) -> None:
        if not tenant_id.strip():
            raise ValueError("TENANT_REQUIRED")
        if not instance_id.strip():
            raise ValueError("INTEGRATION_INSTANCE_REQUIRED")
        self._tenant_id = tenant_id
        self._instance_id = instance_id
        self._account_id = account_id
        self._ingress = ingress
        self._adapter = adapter or EmailNormalizedAdapter()

    def ingest(
        self,
        message: Message,
        *,
        message_ref: str,
        thread_id: str | None = None,
        received_at: datetime | None = None,
    ) -> IntegrationIngressReceipt:
        snapshot = snapshot_rfc822_message(
            message,
            message_ref=message_ref,
            thread_id=thread_id,
        )
        try:
            output = self._adapter.normalize(
                snapshot_to_adapter_input(snapshot),
                tenant_id=self._tenant_id,
                instance_id=self._instance_id,
                account_id=self._account_id,
                received_at=received_at,
            )
        except AdapterNormalizationError as exc:
            raise EmailConnectorError(str(exc)) from None
        if output.artifact_receipts:
            raise EmailConnectorError(
                "EMAIL_ARTIFACT_TRANSPORT_NOT_IMPLEMENTED"
            )
        return self._ingress.send(output.event)


def deterministic_message_ref(*parts: str) -> str:
    """Opaque connector-side reference; never a body or provider credential."""

    if not parts or any(not part for part in parts):
        raise ValueError("EMAIL_MESSAGE_REF_PART_REQUIRED")
    digest = hashlib.sha256(
        "|".join(parts).encode("utf-8")
    ).hexdigest()
    return f"email-ref:{digest}"


__all__ = [
    "EmailConnector",
    "EmailConnectorError",
    "EmailMessageSnapshot",
    "deterministic_message_ref",
    "snapshot_rfc822_message",
    "snapshot_to_adapter_input",
]
