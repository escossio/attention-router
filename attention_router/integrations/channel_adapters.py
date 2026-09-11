"""Reference channel adapters proving the provider-neutral Integration Contract.

These adapters are intentionally transport-facing and side-effect free. They accept
provider-normalized payloads plus trusted runtime tenancy/account context and emit the
same Integration Contract models. Public/provider payloads are never allowed to choose
a tenant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Any, Protocol

from attention_router.contracts.integration import (
    ArtifactReceiptContract,
    ExternalActorRef,
    ExternalThreadRef,
    InboundIntegrationEvent,
    IntegrationKind,
    IntegrationSource,
    IntegrationThreadKind,
)


class AdapterNormalizationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ChannelAdapterOutput:
    event: InboundIntegrationEvent
    artifact_receipts: tuple[ArtifactReceiptContract, ...] = ()


class ChannelInboundAdapter(Protocol):
    integration_name: str

    def normalize(
        self,
        raw_event: dict[str, Any],
        *,
        tenant_id: str,
        instance_id: str,
        account_id: str | None = None,
        received_at: datetime | None = None,
    ) -> ChannelAdapterOutput: ...


def _required(value: Any, reason: str) -> str:
    if value is None:
        raise AdapterNormalizationError(reason)
    normalized = str(value).strip()
    if not normalized:
        raise AdapterNormalizationError(reason)
    return normalized


def _forbid_untrusted_tenant(raw_event: dict[str, Any]) -> None:
    if "tenant_id" in raw_event:
        raise AdapterNormalizationError("UNTRUSTED_TENANT_FIELD_FORBIDDEN")


def _as_utc(value: Any, reason: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AdapterNormalizationError(reason) from exc
    else:
        raise AdapterNormalizationError(reason)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AdapterNormalizationError(reason)
    return parsed.astimezone(timezone.utc)


def _received(value: datetime | None) -> datetime:
    return _as_utc(value or datetime.now(timezone.utc), "ADAPTER_RECEIVED_AT_INVALID")


def _source(name: str, instance_id: str, account_id: str | None) -> IntegrationSource:
    return IntegrationSource(
        kind=IntegrationKind.CHANNEL,
        name=name,
        instance_id=_required(instance_id, "INTEGRATION_INSTANCE_REQUIRED"),
        account_id=account_id.strip() if account_id else None,
    )


def _correlation_id(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


class WhatsAppNormalizedAdapter:
    """Adapter for the normalized payload emitted by the current local WWEBJS bridge."""

    integration_name = "channel.whatsapp"

    def normalize(
        self,
        raw_event: dict[str, Any],
        *,
        tenant_id: str,
        instance_id: str,
        account_id: str | None = None,
        received_at: datetime | None = None,
    ) -> ChannelAdapterOutput:
        _forbid_untrusted_tenant(raw_event)
        tenant_id = _required(tenant_id, "TENANT_REQUIRED")
        external_event_id = _required(
            raw_event.get("external_event_id"), "WHATSAPP_EXTERNAL_EVENT_ID_REQUIRED"
        )
        actor_id = _required(
            raw_event.get("external_actor_id"), "WHATSAPP_EXTERNAL_ACTOR_REQUIRED"
        )
        occurred_at = _as_utc(
            raw_event.get("occurred_at"), "WHATSAPP_OCCURRED_AT_INVALID"
        )
        source = _source(self.integration_name, instance_id, account_id)
        metadata = dict(raw_event.get("metadata") or {})
        identity = dict(raw_event.get("identity") or {})
        message_type = str(raw_event.get("message_type") or "unknown")[:80]
        thread_id = str(metadata.get("thread_id") or actor_id)
        from_me = bool(metadata.get("from_me", False))
        has_media = bool(raw_event.get("has_media", metadata.get("has_media", False)))
        idempotency_key = str(
            identity.get("idempotency_key")
            or f"{source.instance_id}:{external_event_id}"
        )
        correlation_id = str(
            raw_event.get("correlation_id")
            or _correlation_id(tenant_id, source.instance_id, external_event_id)
        )

        event = InboundIntegrationEvent(
            tenant_id=tenant_id,
            source=source,
            external_event_id=external_event_id,
            event_type=str(raw_event.get("event_type") or "message")[:120],
            actor=ExternalActorRef(
                external_actor_id=actor_id,
                display_name=(
                    str(raw_event.get("actor_display_name"))[:160]
                    if raw_event.get("actor_display_name")
                    else None
                ),
            ),
            thread=ExternalThreadRef(
                external_thread_id=thread_id,
                kind=IntegrationThreadKind.DIRECT,
            ),
            payload_type="WHATSAPP_MESSAGE_REFERENCE",
            payload_ref={"provider_event_ref": external_event_id},
            occurred_at=occurred_at,
            received_at=_received(received_at),
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            metadata_sanitized={
                "message_type": message_type,
                "has_media": has_media,
                "from_me": from_me,
                "body_present": bool(raw_event.get("content")),
            },
        )
        return ChannelAdapterOutput(event=event)


class EmailNormalizedAdapter:
    """Provider-neutral email adapter over a normalized message + staged attachments."""

    integration_name = "channel.email"

    def normalize(
        self,
        raw_event: dict[str, Any],
        *,
        tenant_id: str,
        instance_id: str,
        account_id: str | None = None,
        received_at: datetime | None = None,
    ) -> ChannelAdapterOutput:
        _forbid_untrusted_tenant(raw_event)
        tenant_id = _required(tenant_id, "TENANT_REQUIRED")
        message_id = _required(raw_event.get("message_id"), "EMAIL_MESSAGE_ID_REQUIRED")
        thread_id = _required(
            raw_event.get("thread_id") or message_id, "EMAIL_THREAD_ID_REQUIRED"
        )
        sender = raw_event.get("from") or {}
        sender_address = _required(sender.get("address"), "EMAIL_SENDER_REQUIRED")
        sent_at = _as_utc(raw_event.get("sent_at"), "EMAIL_SENT_AT_INVALID")
        source = _source(self.integration_name, instance_id, account_id)
        correlation_id = str(
            raw_event.get("correlation_id")
            or _correlation_id(tenant_id, source.instance_id, message_id)
        )
        staged_attachments = list(raw_event.get("attachments") or [])
        actor = ExternalActorRef(
            external_actor_id=sender_address,
            display_name=(str(sender.get("name"))[:160] if sender.get("name") else None),
        )

        receipts: list[ArtifactReceiptContract] = []
        for index, attachment in enumerate(staged_attachments):
            receipt_id = _required(
                attachment.get("external_receipt_id")
                or attachment.get("attachment_id")
                or f"{message_id}:{index}",
                "EMAIL_ATTACHMENT_RECEIPT_ID_REQUIRED",
            )
            receipts.append(
                ArtifactReceiptContract(
                    tenant_id=tenant_id,
                    source=source,
                    external_receipt_id=receipt_id,
                    content_sha256=_required(
                        attachment.get("content_sha256"),
                        "EMAIL_ATTACHMENT_SHA256_REQUIRED",
                    ),
                    artifact_kind=_required(
                        attachment.get("artifact_kind") or "document",
                        "EMAIL_ATTACHMENT_KIND_REQUIRED",
                    ),
                    mime_type=_required(
                        attachment.get("mime_type"), "EMAIL_ATTACHMENT_MIME_REQUIRED"
                    ),
                    size_bytes=int(attachment.get("size_bytes", -1)),
                    storage_provider=_required(
                        attachment.get("storage_provider"),
                        "EMAIL_ATTACHMENT_STORAGE_PROVIDER_REQUIRED",
                    ),
                    storage_reference=_required(
                        attachment.get("storage_reference"),
                        "EMAIL_ATTACHMENT_STORAGE_REFERENCE_REQUIRED",
                    ),
                    received_at=_received(received_at),
                    sender=actor,
                    original_filename=(
                        str(attachment.get("original_filename"))[:512]
                        if attachment.get("original_filename")
                        else None
                    ),
                    receipt_metadata={"attachment_index": index},
                )
            )

        event = InboundIntegrationEvent(
            tenant_id=tenant_id,
            source=source,
            external_event_id=message_id,
            event_type="message",
            actor=actor,
            thread=ExternalThreadRef(
                external_thread_id=thread_id,
                kind=IntegrationThreadKind.THREAD,
                title=None,
            ),
            payload_type="EMAIL_MESSAGE_REFERENCE",
            payload_ref={"message_ref": str(raw_event.get("message_ref") or message_id)},
            occurred_at=sent_at,
            received_at=_received(received_at),
            idempotency_key=str(
                raw_event.get("idempotency_key")
                or f"{source.instance_id}:{message_id}"
            ),
            correlation_id=correlation_id,
            metadata_sanitized={
                "subject_present": bool(raw_event.get("subject")),
                "body_present": bool(raw_event.get("body_text") or raw_event.get("body_ref")),
                "attachment_count": len(receipts),
                "recipient_count": len(raw_event.get("to") or []),
            },
        )
        return ChannelAdapterOutput(event=event, artifact_receipts=tuple(receipts))


__all__ = [
    "AdapterNormalizationError",
    "ChannelAdapterOutput",
    "ChannelInboundAdapter",
    "EmailNormalizedAdapter",
    "WhatsAppNormalizedAdapter",
]
