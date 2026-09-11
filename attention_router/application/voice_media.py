from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import or_, select

from attention_router.application.voice_transcription import is_voice_input_event
from attention_router.config import settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.media_store import ALLOWED_MIME_TYPES, MediaStore, MediaStoreError
from attention_router.infrastructure.models import (
    InboundEventRow,
    MediaArtifactRow,
    QueueRow,
    VoiceTranscriptionRow,
)
from attention_router.infrastructure.repository import audit


class MediaNotificationRetryable(RuntimeError):
    pass


class MediaReadyNotification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Compatibility default for direct/internal callers. The authenticated media
    # HTTP endpoint rejects notifications that omit tenant_id before validation.
    tenant_id: str = Field(default=DEFAULT_TENANT_ID, min_length=1, max_length=64)
    source: str = Field(min_length=1, max_length=32)
    external_event_id: str = Field(min_length=1, max_length=180)
    media_ref: str | None = Field(default=None, max_length=80)
    content_sha256: str | None = Field(default=None, max_length=64)
    mime_type: str | None = Field(default=None, max_length=80)
    size_bytes: int | None = Field(default=None, ge=0)
    media_kind: str = Field(min_length=1, max_length=32)
    capture_status: str = Field(pattern="^(READY|FAILED|MISSING)$")

    @model_validator(mode="after")
    def validate_ready(self):
        if self.source != "wwebjs":
            raise ValueError("unsupported source")
        if self.capture_status == "READY" and any(
            value is None
            for value in (self.media_ref, self.content_sha256, self.mime_type, self.size_bytes)
        ):
            raise ValueError("ready media metadata required")
        return self


def record_media_notification(session, notification: MediaReadyNotification) -> MediaArtifactRow | None:
    events = list(session.scalars(
        select(InboundEventRow).where(
            InboundEventRow.tenant_id == notification.tenant_id,
            InboundEventRow.source == notification.source,
            InboundEventRow.external_event_id == notification.external_event_id,
        )
    ).all())
    if not events:
        raise MediaNotificationRetryable("INBOUND_EVENT_NOT_COMMITTED")
    if len(events) != 1:
        raise ValueError("INBOUND_EVENT_SCOPE_AMBIGUOUS")
    event = events[0]
    if not is_voice_input_event(event):
        raise ValueError("MEDIA_EVENT_NOT_VOICE")
    existing = session.scalar(
        select(MediaArtifactRow).where(
            MediaArtifactRow.inbound_event_id == event.id,
            MediaArtifactRow.purpose == "VOICE_INPUT",
        )
    )
    transcription = session.scalar(
        select(VoiceTranscriptionRow).where(VoiceTranscriptionRow.inbound_event_id == event.id)
    )
    stamp = now_utc()
    if notification.capture_status != "READY":
        if transcription is None:
            transcription = VoiceTranscriptionRow(
                id=new_id(), tenant_id=event.tenant_id, inbound_event_id=event.id,
                media_artifact_id=None, status="FAILED", transcript_text=None,
                provider=None, model=None, provider_request_reference=None,
                error_code=f"VOICE_MEDIA_{notification.capture_status}", attempt_count=0,
                claimed_at=None, claimed_by=None, created_at=stamp, updated_at=stamp,
                completed_at=stamp,
            )
            session.add(transcription)
        queue = session.get(QueueRow, f"decision:{event.id}")
        if queue:
            queue.status = "CANCELED"
            queue.processed_at = stamp
        audit(session, event.interaction_id, "voice.media_failed", {
            "capture_status": notification.capture_status,
        }, origin="voice_media", tenant_id=event.tenant_id)
        session.flush()
        return None

    store = MediaStore(settings.whatsapp_media_root, settings.whatsapp_media_max_bytes)
    digest = store.digest_from_ref(notification.media_ref or "")
    if digest != notification.content_sha256:
        raise MediaStoreError("MEDIA_REF_HASH_MISMATCH")
    if notification.mime_type not in ALLOWED_MIME_TYPES:
        raise MediaStoreError("UNSUPPORTED_MEDIA_MIME")
    if not notification.size_bytes or notification.size_bytes > settings.whatsapp_media_max_bytes:
        raise MediaStoreError("INVALID_MEDIA_SIZE")
    if existing:
        if (
            existing.content_sha256 != digest
            or existing.mime_type != notification.mime_type
            or existing.size_bytes != notification.size_bytes
        ):
            raise ValueError("MEDIA_NOTIFICATION_IDEMPOTENCY_CONFLICT")
        return existing
    artifact = MediaArtifactRow(
        id=new_id(), tenant_id=event.tenant_id, direction="INBOUND", purpose="VOICE_INPUT",
        inbound_event_id=event.id, execution_intent_id=None, content_sha256=digest,
        media_kind=notification.media_kind, mime_type=notification.mime_type,
        size_bytes=notification.size_bytes, status="READY", created_at=stamp,
        terminal_at=None, expires_at=None,
        provenance={"source": notification.source, "capture_status": "READY"},
    )
    session.add(artifact)
    session.flush()
    if transcription is None:
        session.add(VoiceTranscriptionRow(
            id=new_id(), tenant_id=event.tenant_id, inbound_event_id=event.id,
            media_artifact_id=artifact.id, status="PENDING", transcript_text=None,
            provider=None, model=None, provider_request_reference=None, error_code=None,
            attempt_count=0, claimed_at=None, claimed_by=None, created_at=stamp,
            updated_at=stamp, completed_at=None,
        ))
    elif transcription.status == "FAILED":
        raise ValueError("MEDIA_NOTIFICATION_AFTER_TERMINAL_FAILURE")
    audit(session, event.interaction_id, "voice.media_ready", {
        "artifact_id": artifact.id,
        "media_kind": artifact.media_kind,
        "mime_type": artifact.mime_type,
        "size_bytes": artifact.size_bytes,
    }, origin="voice_media", tenant_id=event.tenant_id)
    session.flush()
    return artifact


def cleanup_expired_media(session, *, store: MediaStore | None = None, limit: int = 100) -> int:
    storage = store or MediaStore(settings.whatsapp_media_root, settings.whatsapp_media_max_bytes)
    stamp = now_utc()
    rows = list(session.scalars(
        select(MediaArtifactRow).where(
            MediaArtifactRow.status.in_(["READY", "AMBIGUOUS"]),
            MediaArtifactRow.terminal_at.is_not(None),
            MediaArtifactRow.expires_at.is_not(None),
            MediaArtifactRow.expires_at <= stamp,
        ).limit(limit)
    ).all())
    deleted = 0
    for artifact in rows:
        active_reference = session.scalar(
            select(MediaArtifactRow.id).where(
                MediaArtifactRow.content_sha256 == artifact.content_sha256,
                MediaArtifactRow.id != artifact.id,
                MediaArtifactRow.status != "DELETED",
                or_(
                    MediaArtifactRow.terminal_at.is_(None),
                    MediaArtifactRow.expires_at.is_(None),
                    MediaArtifactRow.expires_at > stamp,
                ),
            ).limit(1)
        )
        if active_reference is None:
            storage.delete(f"sha256:{artifact.content_sha256}")
        artifact.status = "DELETED"
        deleted += 1
    session.flush()
    return deleted


def mark_artifact_terminal(artifact: MediaArtifactRow, *, ambiguous: bool = False) -> None:
    stamp = now_utc()
    artifact.terminal_at = stamp
    hours = settings.media_ambiguous_retention_hours if ambiguous else settings.media_retention_hours
    artifact.expires_at = stamp + timedelta(hours=hours)
    if ambiguous:
        artifact.status = "AMBIGUOUS"
