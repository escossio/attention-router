"""Bounded evidence-only recovery using the existing queue and STT worker."""

import base64
import json
import logging
import os
from attention_router.application.voice_identity import resolve
from attention_router.application.voice_stt_fallback import transcribe_consensus
from attention_router.application.speech_transcription import InternalSpeechTranscriber
import threading
import time
from datetime import timedelta
from urllib.parse import urlsplit, quote
from urllib.request import Request, urlopen
from sqlalchemy import select, exists, func
from attention_router.config import settings
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    InboundEventRow,
    ConversationMessageRow,
    VoiceTranscriptionRow,
    QueueRow,
    MediaArtifactRow,
)
from attention_router.infrastructure.media_store import MediaStore, media_ref
from attention_router.integrations.whatsapp_history import WhatsAppHistoryAdapter
from attention_router.application.voice_media import (
    MediaReadyNotification,
    record_media_notification,
)
from attention_router.application.voice_transcription import process_voice_transcriptions
from attention_router.application.conversation_live_capture import (
    capture_live_receipt,
    capture_voice_transcription,
)

KIND = "voice_history_recovery_v1"
LOG = logging.getLogger(__name__)


def history_interval_seconds():
    """Bound historical STT pacing. Preserve previous behavior by default."""
    raw = os.environ.get("WHATSAPP_VOICE_BACKFILL_INTERVAL_SECONDS", "60")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 60
    return max(10, min(60, value))


def valid(row):
    return row is not None and row.status == "READY" and bool((row.transcript_text or "").strip())


def seed(session):
    # Receipt IDs are canonical and retain account, peer, time and direction.
    completed = exists().where(
        VoiceTranscriptionRow.inbound_event_id == InboundEventRow.id,
        VoiceTranscriptionRow.status == "READY",
        VoiceTranscriptionRow.transcript_text.is_not(None),
        func.length(func.trim(VoiceTranscriptionRow.transcript_text)) > 0,
    )
    archived = exists().where(
        ConversationMessageRow.metadata_json["source_inbound_event_id"].as_string()
        == InboundEventRow.id,
        ConversationMessageRow.metadata_json["content_state"].as_string() == "TRANSCRIBED",
    )
    completed = completed & archived
    queued = exists().where(QueueRow.id == "voice-history:" + InboundEventRow.id)
    events = session.scalars(
        select(InboundEventRow)
        .where(
            InboundEventRow.source == "wwebjs",
            InboundEventRow.payload["channel"].as_string() == "whatsapp",
            InboundEventRow.payload["metadata"]["message_type"].as_string().in_(["ptt", "audio"]),
            InboundEventRow.received_at < now_utc() - timedelta(minutes=5),
            ~completed,
            ~queued,
        )
        .order_by(InboundEventRow.received_at.desc())
        .limit(25)
    ).all()
    for event in events:
        session.add(
            QueueRow(
                id="voice-history:" + event.id,
                kind=KIND,
                status="PENDING",
                payload={"event_id": event.id, "attempts": 0, "next_at": 0},
                created_at=now_utc(),
            )
        )
    session.commit()


def download(event):
    u = urlsplit(settings.whatsapp_history_url)
    locator, strategy = resolve(event)
    path = "/internal/history/voice-media/" + quote(locator, safe="")
    adapter = WhatsAppHistoryAdapter(
        base_url=settings.whatsapp_history_url, hmac_secret=settings.local_history_hmac_secret
    )
    request = Request(f"{u.scheme}://{u.netloc}{path}", headers=adapter._headers(path))
    maximum = settings.whatsapp_media_max_bytes * 2 + 4096
    with urlopen(request, timeout=25) as response:
        raw = response.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("MEDIA_TOO_LARGE")
    data = json.loads(raw)
    if data.get("status") != "ok":
        raise ValueError(data.get("code", "MEDIA_UNAVAILABLE"))
    if data.get("message_id") != locator:
        raise ValueError("MEDIA_ID_MISMATCH")
    if data.get("from_me") is not ((event.payload.get("metadata") or {}).get("from_me") is True):
        raise ValueError("MEDIA_DIRECTION_MISMATCH")
    metadata = event.payload.get("metadata") or {}
    if metadata.get("source_account") and data.get("source_account") != metadata["source_account"]:
        raise ValueError("MEDIA_ACCOUNT_MISMATCH")
    from datetime import datetime
    stamp = datetime.fromisoformat(event.payload["occurred_at"].replace("Z", "+00:00")).timestamp()
    if type(data.get("timestamp")) is not int or data["timestamp"] <= 0:
        raise ValueError("MEDIA_TIMESTAMP_INVALID")
    content = base64.b64decode(data["data"], validate=True)
    if not content:
        raise ValueError("VOICE_DOWNLOAD_EMPTY")
    # Exact provider identity/account/direction bind the media, not a timestamp guess.
    # WhatsApp can revise its timestamp after the original receipt was persisted.
    return content, data["mimetype"], {
        "transport_timestamp": data["timestamp"],
        "timestamp_delta_seconds": data["timestamp"] - int(stamp),
        "transport_account": data.get("source_account"),
        "transport_id_strategy": data.get("id_strategy"),
        "media_source": data.get("media_source", "WHATSAPP_WEB"),
    }


def cycle(session):
    seed(session)
    # Never start historical STT while live work is queued or processing.
    live = session.scalar(
        select(VoiceTranscriptionRow.id)
        .where(
            VoiceTranscriptionRow.status.in_(["PENDING", "PROCESSING"]),
            ~exists().where(
                QueueRow.id == "voice-history:" + VoiceTranscriptionRow.inbound_event_id
            ),
        )
        .limit(1)
    )
    if live:
        return
    recent = session.scalar(
        select(QueueRow.id)
        .where(
            QueueRow.kind == KIND,
            QueueRow.processed_at > now_utc() - timedelta(seconds=history_interval_seconds()),
        )
        .limit(1)
    )
    if recent:
        return
    jobs = session.scalars(
        select(QueueRow)
        .where(
            QueueRow.kind == KIND,
            QueueRow.status.in_(["PENDING", "PROCESSING"]),
            QueueRow.payload["next_at"].as_float() <= time.time(),
        )
        .order_by(QueueRow.payload["next_at"].as_float(), QueueRow.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).all()
    job = next((j for j in jobs if float(j.payload.get("next_at", 0)) <= time.time()), None)
    if job is None:
        return
    payload = dict(job.payload)
    payload["attempts"] = payload.get("attempts", 0) + 1
    payload["revision_attempts"] = payload.get("revision_attempts", 0) + 1
    payload["next_at"] = time.time() + 600
    job.payload = payload
    job.status = "PROCESSING"
    session.commit()
    try:
        event = session.get(InboundEventRow, payload["event_id"])
        row = session.scalar(
            select(VoiceTranscriptionRow).where(VoiceTranscriptionRow.inbound_event_id == event.id)
        )
        was_failed_empty = (
            row is not None and row.status == "FAILED" and row.error_code == "empty_transcript"
        )
        if not valid(row):
            if (
                row is not None
                and row.status == "PROCESSING"
                and row.claimed_at
                and row.claimed_at > now_utc() - timedelta(minutes=10)
            ):
                raise ValueError("STT_LEASE_ACTIVE")
            artifact = session.scalar(
                select(MediaArtifactRow).where(
                    MediaArtifactRow.inbound_event_id == event.id,
                    MediaArtifactRow.purpose == "VOICE_INPUT",
                )
            )
            store = MediaStore(settings.whatsapp_media_root, settings.whatsapp_media_max_bytes)
            stored = False
            recovery_evidence = {}
            if artifact is not None:
                try:
                    store.validate(
                        media_ref(artifact.content_sha256),
                        expected_sha256=artifact.content_sha256,
                        expected_size=artifact.size_bytes,
                        mime_type=artifact.mime_type,
                    )
                    stored = True
                except Exception:
                    pass
            if not stored:
                content, mime, recovery_evidence = download(event)
                ref, digest, size, _ = store.put_bytes(content, mime_type=mime)
                if artifact is not None and artifact.content_sha256 != digest:
                    raise ValueError("MEDIA_HASH_CHANGED")
                if artifact is None:
                    # Reset a terminal failure only after verified bytes are available.
                    if row is not None:
                        row.status = "PENDING"
                        row.error_code = None
                        session.flush()
                    artifact = record_media_notification(
                        session,
                        MediaReadyNotification(
                            tenant_id=event.tenant_id,
                            source=event.source,
                            external_event_id=event.external_event_id,
                            media_ref=ref,
                            content_sha256=digest,
                            mime_type=mime,
                            size_bytes=size,
                            media_kind=event.payload["metadata"]["message_type"],
                            capture_status="READY",
                        ),
                    )
            provenance = dict(artifact.provenance or {})
            provenance["history_recovery"] = {
                **recovery_evidence,
                "revision": "identity-v2", "strategy": "LOCAL_HASH_VERIFIED" if stored else resolve(event)[1],
                "source_event_id": event.id, "source_message_id": event.external_event_id,
                "source_account": (event.payload.get("metadata") or {}).get("source_account"),
                "from_me": (event.payload.get("metadata") or {}).get("from_me"),
                "occurred_at": event.payload.get("occurred_at"),
                "lookup_id": None if stored else resolve(event)[0],
            }
            artifact.provenance = provenance
            artifact.status = "READY"
            artifact.expires_at = None
            artifact.terminal_at = None
            row = session.scalar(
                select(VoiceTranscriptionRow).where(
                    VoiceTranscriptionRow.inbound_event_id == event.id
                )
            )
            if row is None:
                row = VoiceTranscriptionRow(
                    id=new_id(),
                    tenant_id=event.tenant_id,
                    inbound_event_id=event.id,
                    status="PENDING",
                    attempt_count=0,
                    created_at=now_utc(),
                    updated_at=now_utc(),
                )
                session.add(row)
            row.media_artifact_id = artifact.id
            fallback_allowed = (
                was_failed_empty and artifact.mime_type == "audio/ogg"
                and os.environ.get("WHATSAPP_VOICE_STT_NORMALIZATION_FALLBACK_ENABLED") == "true"
            )
            if fallback_allowed:
                original = store.validate(
                    media_ref(artifact.content_sha256),
                    expected_sha256=artifact.content_sha256,
                    expected_size=artifact.size_bytes,
                    mime_type=artifact.mime_type,
                )
                consensus = transcribe_consensus(
                    original,
                    max_bytes=settings.whatsapp_media_max_bytes,
                    transcriber=InternalSpeechTranscriber(settings=settings),
                )
                if consensus is None:
                    # Nonagreement or empty STT is not a transcript; keep REVIEW_REQUIRED.
                    raise ValueError("NORMALIZED_STT_UNVERIFIED")
                row.status = "READY"
                row.transcript_text = consensus.transcript
                row.provider = consensus.provider
                row.model = consensus.model
                row.provider_request_reference = consensus.request_id
                row.error_code = None
                row.claimed_at = None
                row.claimed_by = None
                row.attempt_count += 2
                row.updated_at = now_utc()
                row.completed_at = row.updated_at
                artifact.terminal_at = row.updated_at
                evidence = dict(artifact.provenance or {})
                evidence["stt_format_fallback"] = {
                    "strategy": "OGG_TO_MP3_TWO_RATE_CONSENSUS_V1",
                    "rates_hz": [24000, 48000],
                    "original_sha256": artifact.content_sha256,
                    "agreement_char": consensus.char_agreement,
                    "agreement_words": consensus.word_agreement,
                    "human_verified": False,
                }
                artifact.provenance = evidence
                session.commit()
            else:
                row.status = "PENDING"
                row.error_code = None
                row.claimed_at = None
                row.claimed_by = None
                session.commit()
                process_voice_transcriptions(session, "voice-history", limit=1, event_id=event.id)
                session.expire_all()
                row = session.get(VoiceTranscriptionRow, row.id)
                if not valid(row):
                    raise ValueError("STT_NOT_READY")
        capture_live_receipt(session, event, historical=True)
        result = capture_voice_transcription(session, event, row, historical=True)
        payload = dict(job.payload)
        payload["revision"] = "identity-v2"
        payload["archive_result"] = result.status
        if result.status != "TRANSCRIPTION_ATTACHED":
            raise ValueError("ARCHIVE_NOT_ATTACHED")
        payload["next_at"] = 0
        payload["error"] = None
        job.payload = payload
        job.status = "DONE"
        job.processed_at = now_utc()
        session.commit()
    except Exception as exc:
        session.rollback()
        job = session.get(QueueRow, job.id)
        payload = dict(job.payload)
        # No provider text, URLs or message identifiers in operational errors.
        safe = (
            str(exc)
            if isinstance(exc, ValueError) and str(exc).replace("_", "").isalnum()
            else type(exc).__name__
        )
        payload["error"] = safe[:80]
        payload["next_at"] = time.time() + min(21600, 60 * 2 ** min(payload["revision_attempts"], 9))
        job.payload = payload
        history = list(payload.get("attempt_history", []))
        history.append({"at": now_utc().isoformat(), "error": safe, "attempt": payload["attempts"]})
        payload["attempt_history"] = history
        payload["revision"] = "identity-v2"
        job.payload = payload
        exhausted = payload["revision_attempts"] >= 5
        media_absent = safe in {"VOICE_MESSAGE_NOT_FOUND", "VOICE_MEDIA_NOT_AVAILABLE", "VOICE_DOWNLOAD_EMPTY"}
        # Two divergent normalized STT hypotheses are terminal for this review
        # attempt. Never requeue automatically or invent a transcript.
        if safe == "NORMALIZED_STT_UNVERIFIED":
            job.status = "REVIEW_REQUIRED"
        else:
            job.status = ("UNAVAILABLE" if media_absent else "REVIEW_REQUIRED") if exhausted else "PENDING"
        session.commit()


def start_voice_backfill():
    if os.environ.get("WHATSAPP_VOICE_BACKFILL_ENABLED") != "true":
        return
    from attention_router.infrastructure.db import SessionLocal
    from attention_router.infrastructure.maintenance_gate import runtime_admission, observe_runtime

    observe_runtime("voice-backfill")

    def run():
        while True:
            try:
                with runtime_admission("voice-backfill") as admitted:
                    if admitted:
                        with SessionLocal() as session:
                            cycle(session)
            except Exception:
                LOG.warning("voice_history_cycle_failed")
            time.sleep(history_interval_seconds())

    threading.Thread(target=run, name="voice-history", daemon=True).start()
