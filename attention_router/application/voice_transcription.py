"""Trusted WhatsApp voice transcription boundary and worker."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib import error, request

from sqlalchemy import select

from attention_router.config import settings
from attention_router.domain.enums import InteractionState
from attention_router.domain.models import now_utc
from attention_router.infrastructure.media_store import MediaStore, MediaStoreError, media_ref
from attention_router.infrastructure.models import (
    InboundEventRow,
    InteractionRow,
    MediaArtifactRow,
    QueueRow,
    VoiceTranscriptionRow,
)
from attention_router.infrastructure.repository import audit


VOICE_MESSAGE_TYPES = frozenset({"audio", "ptt"})


class VoiceTranscriptionError(RuntimeError):
    pass


def _voice_fields(event: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
    payload = getattr(event, "payload", None) or {}
    metadata = payload.get("metadata") or {}
    message_type = str(metadata.get("message_type", payload.get("message_type", ""))).casefold()
    return message_type, payload, metadata


def is_voice_input_event(event: Any) -> bool:
    if event is None or getattr(event, "source", None) != "wwebjs":
        return False
    message_type, payload, _metadata = _voice_fields(event)
    return payload.get("channel") == "whatsapp" and message_type in VOICE_MESSAGE_TYPES


def voice_media_available(event: Any) -> bool:
    if not is_voice_input_event(event):
        return False
    _message_type, payload, metadata = _voice_fields(event)
    return metadata.get("has_media", payload.get("has_media")) is True


def requires_voice_transcription(event: Any) -> bool:
    return is_voice_input_event(event)


def transcription_for_event(session, event_id: str) -> VoiceTranscriptionRow | None:
    return session.scalar(
        select(VoiceTranscriptionRow).where(VoiceTranscriptionRow.inbound_event_id == event_id)
    )


def voice_decision_readiness(session, event: InboundEventRow) -> tuple[str, str]:
    if not is_voice_input_event(event):
        return "READY", "TEXT_INPUT"
    if not voice_media_available(event):
        return "FAILED", "VOICE_MEDIA_MISSING"
    transcription = transcription_for_event(session, event.id)
    if transcription is None or transcription.status in {"PENDING", "PROCESSING"}:
        return "WAITING", "VOICE_TRANSCRIPTION_PENDING"
    if transcription.status != "READY" or not (transcription.transcript_text or "").strip():
        return "FAILED", transcription.error_code or "VOICE_TRANSCRIPTION_FAILED"
    return "READY", "VOICE_TRANSCRIPTION_READY"


def effective_inbound_text(session, event: InboundEventRow, interaction: InteractionRow) -> str:
    if not is_voice_input_event(event):
        return interaction.inbound_text
    transcription = transcription_for_event(session, event.id)
    if transcription is None or transcription.status != "READY":
        raise VoiceTranscriptionError("VOICE_TRANSCRIPTION_NOT_READY")
    text = (transcription.transcript_text or "").strip()
    if not text:
        raise VoiceTranscriptionError("VOICE_TRANSCRIPTION_EMPTY")
    return text


def effective_interaction_text(session, interaction: InteractionRow) -> str | None:
    event = session.scalar(
        select(InboundEventRow)
        .where(InboundEventRow.interaction_id == interaction.id)
        .order_by(InboundEventRow.received_at.desc())
        .limit(1)
    )
    if event is None:
        return interaction.inbound_text
    try:
        return effective_inbound_text(session, event, interaction)
    except VoiceTranscriptionError:
        return None


class SwitcherSTTClient:
    def transcribe(self, path: Path, mime_type: str) -> dict[str, str]:
        if not settings.stt_enabled:
            raise VoiceTranscriptionError("STT_DISABLED")
        if not settings.stt_internal_token:
            raise VoiceTranscriptionError("STT_INTERNAL_TOKEN_MISSING")
        body = path.read_bytes()
        req = request.Request(
            f"{settings.stt_internal_url.rstrip('/')}/transcribe",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {settings.stt_internal_token}",
                "Content-Type": mime_type,
                "Accept": "application/json",
            },
        )
        try:
            with request.urlopen(req, timeout=settings.stt_timeout_seconds) as response:
                raw = response.read(64 * 1024)
        except TimeoutError as exc:
            raise VoiceTranscriptionError("STT_TIMEOUT") from exc
        except error.HTTPError as exc:
            exc.read(4096)
            raise VoiceTranscriptionError(f"STT_HTTP_{exc.code}") from None
        except OSError as exc:
            raise VoiceTranscriptionError("STT_NETWORK_ERROR") from exc
        try:
            value = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise VoiceTranscriptionError("STT_INVALID_RESPONSE") from exc
        transcript = value.get("transcript")
        if value.get("status") != "ok" or not isinstance(transcript, str) or not transcript.strip():
            raise VoiceTranscriptionError("STT_EMPTY_TRANSCRIPT")
        return {
            "transcript": transcript.strip(),
            "provider": str(value.get("provider") or "openai")[:32],
            "model": str(value.get("model") or "")[:80],
            "request_id": str(value.get("request_id") or "")[:180],
        }


def _finish_waiting_queue(session, transcription: VoiceTranscriptionRow) -> None:
    from attention_router.application.owner_reply_grace import grace_allows_interaction

    event = session.get(InboundEventRow, transcription.inbound_event_id)
    interaction = session.get(InteractionRow, event.interaction_id) if event else None
    queue = session.get(QueueRow, f"decision:{transcription.inbound_event_id}")
    if interaction is None or queue is None:
        return
    if interaction.state == InteractionState.CANCELED_BY_HUMAN_REPLY.value:
        queue.status = "CANCELED"
        queue.processed_at = now_utc()
    elif transcription.status == "FAILED":
        queue.status = "CANCELED"
        queue.processed_at = now_utc()
    elif queue.status == "WAITING_TRANSCRIPTION" and grace_allows_interaction(session, interaction.id):
        queue.status = "PENDING"
        queue.processed_at = None


def process_voice_transcriptions(session, worker: str, limit: int = 10, client=None) -> int:
    stamp = now_utc()
    stale_query = select(VoiceTranscriptionRow).where(
        VoiceTranscriptionRow.status == "PROCESSING",
        VoiceTranscriptionRow.claimed_at < stamp - timedelta(
            seconds=settings.stt_processing_lease_seconds
        ),
    ).limit(limit)
    if session.bind and session.bind.dialect.name == "postgresql":
        stale_query = stale_query.with_for_update(skip_locked=True)
    stale_rows = list(session.scalars(stale_query).all())
    for stale in stale_rows:
        stale.status = "PENDING"
        stale.claimed_at = None
        stale.claimed_by = None
        stale.updated_at = stamp
        stale.error_code = "STALE_PROCESSING_RECLAIMED"
    if stale_rows:
        session.commit()
    if client is None and not settings.stt_enabled:
        return 0
    query = (
        select(VoiceTranscriptionRow)
        .where(VoiceTranscriptionRow.status == "PENDING")
        .order_by(VoiceTranscriptionRow.created_at)
        .limit(limit)
    )
    if session.bind and session.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    rows = list(session.scalars(query).all())
    stamp = now_utc()
    for row in rows:
        row.status = "PROCESSING"
        row.claimed_at = stamp
        row.claimed_by = worker
        row.updated_at = stamp
    session.commit()
    stt_client = client or SwitcherSTTClient()
    store = MediaStore(settings.whatsapp_media_root, settings.whatsapp_media_max_bytes)
    completed = 0
    for claimed in rows:
        row = session.get(VoiceTranscriptionRow, claimed.id)
        artifact = session.get(MediaArtifactRow, row.media_artifact_id) if row else None
        try:
            if row is None or artifact is None or artifact.status != "READY":
                raise VoiceTranscriptionError("VOICE_ARTIFACT_NOT_READY")
            reference = media_ref(artifact.content_sha256)
            path = store.validate(
                reference,
                expected_sha256=artifact.content_sha256,
                expected_size=artifact.size_bytes,
                mime_type=artifact.mime_type,
            )
            # attempt_count records provider calls that actually started. A
            # crash after claim but before this boundary does not consume one.
            row.attempt_count += 1
            row.updated_at = now_utc()
            session.commit()
            result = stt_client.transcribe(path, artifact.mime_type)
            row.status = "READY"
            row.transcript_text = result["transcript"]
            row.provider = result["provider"]
            row.model = result["model"]
            row.provider_request_reference = result["request_id"] or None
            row.error_code = None
        except (VoiceTranscriptionError, MediaStoreError) as exc:
            if row is None:
                continue
            row.status = "FAILED"
            row.transcript_text = None
            row.error_code = str(exc)[:120]
        row.claimed_at = None
        row.claimed_by = None
        row.updated_at = now_utc()
        row.completed_at = row.updated_at
        if artifact:
            artifact.terminal_at = row.updated_at
            artifact.expires_at = row.updated_at + timedelta(hours=settings.media_retention_hours)
        _finish_waiting_queue(session, row)
        if artifact:
            event = session.get(InboundEventRow, row.inbound_event_id)
            audit(
                session,
                event.interaction_id if event else None,
                "voice.transcription_completed",
                {"status": row.status, "error_code": row.error_code},
                origin="voice_transcription",
                tenant_id=row.tenant_id,
            )
        session.commit()
        completed += 1
    return completed
