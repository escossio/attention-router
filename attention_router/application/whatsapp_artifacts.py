"""Canonical Artifact Plane staging for inbound WhatsApp media."""

from __future__ import annotations

from datetime import UTC

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.platform.artifact_storage import (
    ArtifactStageInput,
    ArtifactStageResult,
    stage_artifact_receipt,
)
from attention_router.application.voice_media import MediaReadyNotification
from attention_router.config import settings
from attention_router.infrastructure.artifact_store import LocalArtifactStore
from attention_router.infrastructure.media_store import MediaStore
from attention_router.infrastructure.models import InboundEventRow


class WhatsAppArtifactRetryable(RuntimeError):
    pass


def _artifact_kind(mime_type: str) -> str:
    normalized = mime_type.casefold()
    if normalized.startswith("image/"):
        return "IMAGE"
    if normalized.startswith("audio/"):
        return "AUDIO"
    if normalized.startswith("video/"):
        return "VIDEO"
    return "DOCUMENT"


def _source_account(event: InboundEventRow) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    value = str(metadata.get("source_account") or "default").strip()
    if not 1 <= len(value) <= 120:
        raise ValueError("WHATSAPP_ARTIFACT_SOURCE_ACCOUNT_INVALID")
    return value


def _sender_actor_id(event: InboundEventRow) -> str | None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    value = payload.get("actor_id")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if 1 <= len(normalized) <= 120 else None


def stage_whatsapp_artifact_notification(
    session: Session,
    notification: MediaReadyNotification,
    *,
    artifact_store: LocalArtifactStore | None = None,
    media_store: MediaStore | None = None,
) -> ArtifactStageResult | None:
    """Stage READY WhatsApp media as opaque canonical Artifact evidence.

    The signed notification is not authority for source account or sender.
    Those values are derived from the already committed inbound event.
    """
    if not settings.whatsapp_artifact_ingestion_enabled:
        return None
    if notification.capture_status != "READY":
        return None

    events = list(
        session.scalars(
            select(InboundEventRow).where(
                InboundEventRow.tenant_id == notification.tenant_id,
                InboundEventRow.source == notification.source,
                InboundEventRow.external_event_id
                == notification.external_event_id,
            )
        ).all()
    )
    if not events:
        raise WhatsAppArtifactRetryable("INBOUND_EVENT_NOT_COMMITTED")
    if len(events) != 1:
        raise ValueError("INBOUND_EVENT_SCOPE_AMBIGUOUS")
    event = events[0]

    if not notification.media_ref or not notification.content_sha256:
        raise ValueError("WHATSAPP_ARTIFACT_MEDIA_REFERENCE_REQUIRED")
    if not notification.mime_type or not notification.size_bytes:
        raise ValueError("WHATSAPP_ARTIFACT_MEDIA_METADATA_REQUIRED")
    if notification.size_bytes > settings.whatsapp_artifact_max_bytes:
        raise ValueError("WHATSAPP_ARTIFACT_SIZE_EXCEEDED")

    staging = media_store or MediaStore(
        settings.whatsapp_media_root,
        max(
            settings.whatsapp_media_max_bytes,
            settings.whatsapp_artifact_max_bytes,
        ),
    )
    data = staging.read_opaque_bytes(
        notification.media_ref,
        expected_sha256=notification.content_sha256,
        expected_size=notification.size_bytes,
        max_bytes=settings.whatsapp_artifact_max_bytes,
    )
    canonical = artifact_store or LocalArtifactStore(
        settings.artifact_store_root,
        max_bytes=settings.artifact_store_max_bytes,
    )
    received_at = event.received_at
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        received_at = received_at.replace(tzinfo=UTC)
    else:
        received_at = received_at.astimezone(UTC)
    return stage_artifact_receipt(
        session,
        canonical,
        ArtifactStageInput(
            tenant_id=event.tenant_id,
            data=data,
            artifact_kind=_artifact_kind(notification.mime_type),
            mime_type=notification.mime_type,
            source_channel="channel.whatsapp",
            external_receipt_id=(
                f"whatsapp:{event.external_event_id}:media"
            ),
            received_at=received_at,
            source_account=_source_account(event),
            sender_actor_id=_sender_actor_id(event),
            original_filename=notification.original_filename,
            receipt_metadata={
                "media_kind": notification.media_kind,
                "capture_status": notification.capture_status,
                "original_filename": notification.original_filename,
            },
        ),
    )


__all__ = [
    "WhatsAppArtifactRetryable",
    "stage_whatsapp_artifact_notification",
]
