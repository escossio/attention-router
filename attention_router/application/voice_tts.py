from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from attention_router.adapters.tts import SwitcherTTSClient, TTSClientError
from attention_router.application.conversation_language import resolve_interaction_locale_by_id
from attention_router.application.voice_media import mark_artifact_terminal
from attention_router.application.voice_transcription import is_voice_input_event
from attention_router.config import settings
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.media_store import MediaStore, MediaStoreError, media_ref
from attention_router.infrastructure.audio_normalizer import normalize_voice_for_whatsapp
from attention_router.infrastructure.models import (
    AgentDecisionRow,
    AgentExecutionIntentRow,
    InboundEventRow,
    InteractionRow,
    MediaArtifactRow,
    OutboxMessageRow,
    TTSDerivationRow,
)
from attention_router.infrastructure.repository import audit


def source_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def intent_is_voice_response(session, intent: AgentExecutionIntentRow) -> bool:
    decision = session.get(AgentDecisionRow, intent.agent_decision_id)
    event = session.get(InboundEventRow, decision.event_id) if decision else None
    return is_voice_input_event(event)


def ensure_tts_derivation(session, intent: AgentExecutionIntentRow) -> TTSDerivationRow:
    existing = session.scalar(
        select(TTSDerivationRow).where(TTSDerivationRow.execution_intent_id == intent.id)
    )
    digest = source_text_hash(intent.effective_response_snapshot)
    if existing:
        if existing.source_text_hash != digest:
            raise ValueError("TTS_SOURCE_TEXT_CHANGED")
        return existing
    stamp = now_utc()
    row = TTSDerivationRow(
        id=new_id(), execution_intent_id=intent.id, status="PENDING",
        source_text_hash=digest, media_artifact_id=None, provider=None,
        request_reference=None, error_code=None, attempt_count=0,
        claimed_at=None, claimed_by=None, created_at=stamp, updated_at=stamp,
        completed_at=None,
    )
    session.add(row)
    session.flush()
    return row


def process_tts_derivations(session, worker: str, limit: int = 10, client=None) -> int:
    stamp = now_utc()
    stale_query = select(TTSDerivationRow).where(
        TTSDerivationRow.status == "PROCESSING",
        TTSDerivationRow.claimed_at < stamp - timedelta(
            seconds=settings.tts_processing_lease_seconds
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
    if client is None and not settings.tts_enabled:
        return 0
    query = (
        select(TTSDerivationRow)
        .where(TTSDerivationRow.status == "PENDING")
        .order_by(TTSDerivationRow.created_at)
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
    provider = client or SwitcherTTSClient()
    store = MediaStore(settings.whatsapp_media_root, settings.whatsapp_media_max_bytes)
    completed = 0
    for claimed in rows:
        row = session.get(TTSDerivationRow, claimed.id)
        intent = session.get(AgentExecutionIntentRow, row.execution_intent_id) if row else None
        decision = session.get(AgentDecisionRow, intent.agent_decision_id) if intent else None
        interaction = session.get(InteractionRow, decision.interaction_id) if decision else None
        temp_path: Path | None = None
        provider_attempted = False
        try:
            if intent is None or decision is None or interaction is None or not intent_is_voice_response(session, intent):
                raise TTSClientError("tts_intent_invalid")
            if source_text_hash(intent.effective_response_snapshot) != row.source_text_hash:
                raise TTSClientError("tts_source_text_mismatch")
            fd, raw_path = tempfile.mkstemp(prefix="andy-tts-", suffix=".mp3")
            os.close(fd)
            temp_path = Path(raw_path)
            # attempt_count records provider calls that actually started. A
            # crash after claim but before this boundary does not consume one.
            row.attempt_count += 1
            row.updated_at = now_utc()
            session.commit()
            provider_attempted = True
            resolved_locale = resolve_interaction_locale_by_id(session, interaction.id)
            result = provider.synthesize(
                intent.effective_response_snapshot,
                temp_path,
                language=resolved_locale.locale,
            )
            audio = temp_path.read_bytes()
            provider_output_mime = getattr(result, "output_mime", "audio/mpeg")
            normalized = normalize_voice_for_whatsapp(audio, provider_output_mime)
            reference, digest, size, _stored = store.put_bytes(
                normalized.data, mime_type=normalized.mime_type
            )
            artifact = session.scalar(select(MediaArtifactRow).where(
                MediaArtifactRow.execution_intent_id == intent.id,
                MediaArtifactRow.purpose == "TTS_OUTPUT",
            ))
            if artifact is None:
                artifact = MediaArtifactRow(
                    id=new_id(), tenant_id=interaction.tenant_id, direction="OUTBOUND",
                    purpose="TTS_OUTPUT", inbound_event_id=None,
                    execution_intent_id=intent.id, content_sha256=digest,
                    media_kind="ptt", mime_type=normalized.mime_type, size_bytes=size,
                    status="READY", created_at=now_utc(), terminal_at=None,
                    expires_at=None, provenance={
                        "profile": "andy",
                        "provider_output_mime": provider_output_mime,
                        "delivery_mime": normalized.mime_type,
                        "normalization": normalized.metadata,
                    },
                )
                session.add(artifact)
                session.flush()
            row.status = "READY"
            row.media_artifact_id = artifact.id
            row.provider = result.provider or "xai"
            row.request_reference = result.request_id
            row.error_code = None
            outbox = session.scalar(select(OutboxMessageRow).where(
                OutboxMessageRow.execution_intent_id == intent.id
            ))
            if outbox is None:
                from attention_router.application.execution import resolve_recipient
                recipient = resolve_recipient(session, intent)
                outbox = OutboxMessageRow(
                    id=new_id(), interaction_id=interaction.id,
                    action_type="agent_execution_voice", destination="local_transport",
                    payload={
                        "external_actor_id": recipient.reference,
                        "media_ref": reference,
                        "content_sha256": digest,
                        "mime_type": normalized.mime_type,
                        "size_bytes": size,
                        "execution_intent_id": intent.id,
                        "message_type": "ptt",
                    },
                    status="PENDING", created_at=now_utc(), available_at=now_utc(),
                    attempt_count=0, idempotency_key=f"execution:{intent.id}",
                    correlation_id=interaction.correlation_id, causation_id=decision.id,
                    execution_intent_id=intent.id,
                )
                session.add(outbox)
            intent.status = "QUEUED"
            intent.blocked_reason = None
            audit(session, interaction.id, "tts.derivation_ready", {
                "tts_derivation_id": row.id, "artifact_id": artifact.id,
            }, origin="voice_tts")
        except (TTSClientError, MediaStoreError, OSError, ValueError) as exc:
            if provider_attempted and row.attempt_count <= 1:
                row.status = "PENDING"
                row.error_code = str(exc)[:120]
            else:
                row.status = "FAILED"
                row.error_code = "TTS_DERIVATION_FAILED"
                row.completed_at = now_utc()
                if intent:
                    intent.status = "BLOCKED"
                    intent.blocked_reason = "TTS_DERIVATION_FAILED"
                if interaction:
                    audit(session, interaction.id, "tts.derivation_failed", {
                        "reason": "TTS_DERIVATION_FAILED",
                    }, origin="voice_tts")
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        row.claimed_at = None
        row.claimed_by = None
        row.updated_at = now_utc()
        if row.status == "READY":
            row.completed_at = row.updated_at
        session.commit()
        completed += 1
    return completed


def validate_voice_outbox(session, outbox: OutboxMessageRow, intent: AgentExecutionIntentRow) -> None:
    if outbox.action_type != "agent_execution_voice" or outbox.destination != "local_transport":
        raise ValueError("VOICE_OUTBOX_CONTRACT_MISMATCH")
    derivation = session.scalar(select(TTSDerivationRow).where(
        TTSDerivationRow.execution_intent_id == intent.id
    ))
    artifact = session.get(MediaArtifactRow, derivation.media_artifact_id) if derivation else None
    if derivation is None or derivation.status != "READY" or artifact is None or artifact.status != "READY":
        raise ValueError("VOICE_DERIVATION_NOT_READY")
    if derivation.source_text_hash != source_text_hash(intent.effective_response_snapshot):
        raise ValueError("VOICE_SOURCE_TEXT_MISMATCH")
    payload = outbox.payload or {}
    if (
        payload.get("execution_intent_id") != intent.id
        or payload.get("content_sha256") != artifact.content_sha256
        or payload.get("mime_type") != artifact.mime_type
        or payload.get("size_bytes") != artifact.size_bytes
        or payload.get("media_ref") != media_ref(artifact.content_sha256)
    ):
        raise ValueError("VOICE_OUTBOX_ARTIFACT_MISMATCH")
    MediaStore(settings.whatsapp_media_root, settings.whatsapp_media_max_bytes).validate(
        payload["media_ref"], expected_sha256=artifact.content_sha256,
        expected_size=artifact.size_bytes, mime_type=artifact.mime_type,
    )


def mark_voice_outbox_terminal(session, outbox: OutboxMessageRow, *, ambiguous: bool = False) -> None:
    derivation = session.scalar(select(TTSDerivationRow).where(
        TTSDerivationRow.execution_intent_id == outbox.execution_intent_id
    ))
    artifact = session.get(MediaArtifactRow, derivation.media_artifact_id) if derivation else None
    if artifact:
        mark_artifact_terminal(artifact, ambiguous=ambiguous)
