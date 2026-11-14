"""Archive observed, new WhatsApp conversations without activating memory/answers.

Only existing, signed-ingress receipts are eligible. This module has no model
dependency, network I/O, execution capability or history-import logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, literal, or_, select
from sqlalchemy.orm import Session

from attention_router.application.memory import (
    ArchivedMessageInput,
    _normalize,
    _redact,
    archive_message,
)
from attention_router.application.platform.context import resolve_represented_subject
from attention_router.config import settings
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.artifact_models import (
    ArtifactReceiptRow,
    ArtifactRow,
    ArtifactUnderstandingRow,
)
from attention_router.infrastructure.models import (
    ConversationMessageRow,
    ConversationThreadRow,
    InboundEventRow,
    VoiceTranscriptionRow,
)


_CAPTURE_ORIGINS = frozenset({
    "EXTERNAL_INBOUND",
    "OWNER_MANUAL_OUTBOUND_OBSERVED",
    "ROUTER_AUTOMATED_OUTBOUND_OBSERVED",
    "UNKNOWN_FROM_ME",
})
_VOICE_TYPES = frozenset({"ptt", "audio"})
_TEXT_TYPES = frozenset({"chat", "text"})
_MEDIA_TYPES = {
    "image": "IMAGE",
    "document": "DOCUMENT",
    "video": "VIDEO",
}
_STORED_MEDIA_KINDS = frozenset({"IMAGE", "DOCUMENT", "VIDEO", "VOICE"})
_MEDIA_CONFLICT_CODES = frozenset({
    "ARTIFACT_CONVERSATION_SCOPE_CONFLICT",
    "ARTIFACT_UNDERSTANDING_KIND_CONFLICT",
    "ARTIFACT_REFERENCE_CONFLICT",
    "ARTIFACT_RECEIPT_MISSING",
    "ARTIFACT_KIND_OR_TENANT_CONFLICT",
    "ARTIFACT_UNDERSTANDING_LINK_CONFLICT",
})


@dataclass(frozen=True)
class ConversationCaptureResult:
    status: str
    message_id: str | None = None
    reason: str = ""


class ConversationCaptureConflict(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _start_allowed(receipt: InboundEventRow) -> bool:
    if not settings.conversation_capture_enabled:
        return False
    start = settings.conversation_capture_start_at
    if start is None or start.tzinfo is None or start.utcoffset() is None:
        return False
    if settings.conversation_capture_tenant_id and (
        receipt.tenant_id != settings.conversation_capture_tenant_id
    ):
        return False
    return _utc(receipt.received_at) >= _utc(start)


def _fields(receipt: InboundEventRow) -> dict[str, Any] | None:
    """Fail closed on missing conversation, sender or organic event boundaries."""
    if (
        not _start_allowed(receipt)
        or receipt.source != "wwebjs"
        or receipt.event_type != "message"
        or receipt.lineage_classification != "ORGANIC"
    ):
        return None
    payload = receipt.payload or {}
    metadata = payload.get("metadata")
    if (
        not isinstance(metadata, dict)
        or payload.get("channel") != "whatsapp"
        or metadata.get("conversation_state") != "READY"
    ):
        return None
    origin = payload.get("event_origin")
    from_me = metadata.get("from_me")
    if origin not in _CAPTURE_ORIGINS:
        return None
    if origin == "EXTERNAL_INBOUND":
        if from_me is not False or payload.get("owner_authenticated") is True:
            return None
    elif (
        from_me is not True
        or payload.get("owner_authenticated") is True
        or metadata.get("from_me_classification") != origin
    ):
        return None

    conversation = metadata.get("conversation_key")
    account = metadata.get("source_account")
    if (
        not isinstance(conversation, str)
        or not conversation.startswith("wwebjs:")
        or len(conversation) > 240
        or not isinstance(account, str)
        or not account.strip()
        or len(account) > 180
    ):
        return None
    raw_kind = metadata.get("message_type")
    if not isinstance(raw_kind, str):
        return None
    kind = raw_kind.casefold()
    if kind not in _TEXT_TYPES | _VOICE_TYPES | _MEDIA_TYPES.keys():
        return None
    if kind in _MEDIA_TYPES and metadata.get("has_media") is not True:
        # A media-type hint alone does not prove that a file was observed.
        return None

    occurred_at = payload.get("occurred_at")
    stamp = None
    provider_timestamp_used = False
    if isinstance(occurred_at, str):
        try:
            parsed = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                stamp = _utc(parsed)
                provider_timestamp_used = True
        except ValueError:
            pass
    if stamp is None:
        stamp = _utc(receipt.received_at)

    sender_key = payload.get("actor_id") if origin == "EXTERNAL_INBOUND" else None
    speaker_basis = "EXTERNAL_SENDER" if sender_key else "UNRESOLVED"
    if origin == "EXTERNAL_INBOUND" and metadata.get("is_group") is True:
        sender_key = metadata.get("group_participant_id")
        speaker_basis = "GROUP_PARTICIPANT" if sender_key else "UNRESOLVED"
    if not isinstance(sender_key, str) or not (0 < len(sender_key) <= 120):
        sender_key = None
        speaker_basis = "UNRESOLVED"

    content = payload.get("content")
    if not isinstance(content, str):
        content = ""
    # Some outbound observations are event-only. Never present a placeholder
    # or a missing media transcript as something a human actually said.
    if content.strip() == "[owner outbound observation]":
        content = ""
    # For media, the original caption is conversation text. It is NOT OCR,
    # a visual fact, or permission to display the Artifact's contents.
    text = content.strip() if kind in _TEXT_TYPES or kind in _MEDIA_TYPES else ""
    message_kind = (
        "VOICE" if kind in _VOICE_TYPES
        else _MEDIA_TYPES.get(kind, "TEXT")
    )
    if message_kind == "VOICE":
        state = "PENDING_TRANSCRIPTION"
    elif message_kind in _STORED_MEDIA_KINDS:
        state = "CAPTION_AVAILABLE" if text else "CAPTION_UNAVAILABLE"
    else:
        state = "TEXT_AVAILABLE" if text else "TEXT_UNAVAILABLE"
    raw_quote = metadata.get("quoted_message_id")
    quote = raw_quote if isinstance(raw_quote, str) and len(raw_quote) <= 240 else None
    return {
        "origin": origin,
        "from_me": from_me,
        "source_account": account,
        "conversation_key": conversation,
        "thread_type": "GROUP" if metadata.get("is_group") is True else "DIRECT",
        "source_message_id": receipt.external_event_id,
        "sender_key": sender_key,
        "speaker_basis": speaker_basis,
        "text": text or None,
        "message_kind": message_kind,
        "content_state": state,
        "sent_at": stamp,
        "timestamp_basis": "PROVIDER_OCCURRED_AT" if provider_timestamp_used else "RECEIPT_FALLBACK",
        "reply_reference": quote,
        "has_media": metadata.get("has_media") is True,
    }


def capture_live_receipt(
    session: Session,
    receipt: InboundEventRow,
) -> ConversationCaptureResult:
    """Materialize one trustworthy source event in the existing conversation archive.

    Replays are deduplicated by the existing tenant/source/account/message key.
    Ingestion does not enqueue memory extraction or model calls.
    """
    details = _fields(receipt)
    if details is None:
        return ConversationCaptureResult("SKIPPED", reason="NOT_ELIGIBLE")
    sender_key = details["sender_key"]
    if details["origin"] == "OWNER_MANUAL_OUTBOUND_OBSERVED":
        represented = resolve_represented_subject(session, receipt.tenant_id)
        if represented is not None:
            sender_key = represented.entity_id
            details["speaker_basis"] = "REPRESENTED_OWNER"
    metadata = {
        "capture_contract": "LIVE_CONVERSATION_ARCHIVE_V0",
        "source_inbound_event_id": receipt.id,
        "source_received_at": _utc(receipt.received_at).isoformat(),
        "event_origin": details["origin"],
        "speaker_basis": details["speaker_basis"],
        "timestamp_basis": details["timestamp_basis"],
        "content_state": details["content_state"],
        "reply_reference": details["reply_reference"],
    }
    if details["message_kind"] in _STORED_MEDIA_KINDS:
        # The arrival of a WhatsApp message never proves the media bytes
        # were downloaded. Artifact status is attached separately and later.
        metadata["attachment_state"] = (
            "PENDING" if details["has_media"] else "NOT_OBSERVED"
        )
    archived, created = archive_message(
        session,
        ArchivedMessageInput(
            source=receipt.source,
            source_account=details["source_account"],
            thread_key=details["conversation_key"],
            thread_type=details["thread_type"],
            source_message_id=details["source_message_id"],
            sender_key=sender_key,
            sender_display_name=None,
            sent_at=details["sent_at"],
            text=details["text"],
            message_type=details["message_kind"],
            from_me=details["from_me"],
            direction="OUTBOUND" if details["from_me"] else "INBOUND",
            metadata=metadata,
            tenant_id=receipt.tenant_id,
            lineage_classification=receipt.lineage_classification,
        ),
    )
    if not created:
        # A provider ID reused in another chat/account must not be silently
        # credited as continuity in an unrelated conversation.
        parent = session.get(ConversationThreadRow, archived.conversation_id)
        if (
            parent is None
            or parent.tenant_id != receipt.tenant_id
            or parent.external_thread_key != details["conversation_key"]
            or archived.source_account != details["source_account"]
            or archived.from_me != details["from_me"]
            or archived.message_type != details["message_kind"]
        ):
            raise ConversationCaptureConflict("CAPTURE_DUPLICATE_SCOPE_CONFLICT")
    return ConversationCaptureResult(
        "ARCHIVED" if created else "DUPLICATE", message_id=archived.id
    )


def attach_canonical_artifact(
    session: Session,
    receipt: InboundEventRow,
) -> ConversationCaptureResult:
    """Link one already-staged Artifact to its original WhatsApp message.

    The sole authority for a link is the persisted signed ingress event and
    the exact, tenant/account-scoped Artifact receipt. A READY visual
    understanding is evidence, never an authorization or an implicit message.
    No OCR, inference, credential or model output is copied into the archive.
    """
    details = _fields(receipt)
    if (
        details is None
        or details["message_kind"] not in _STORED_MEDIA_KINDS
        or not details["has_media"]
    ):
        return ConversationCaptureResult("SKIPPED", reason="NOT_ELIGIBLE")

    row = session.scalar(select(ConversationMessageRow).where(
        ConversationMessageRow.tenant_id == receipt.tenant_id,
        ConversationMessageRow.source == receipt.source,
        ConversationMessageRow.source_account == details["source_account"],
        ConversationMessageRow.source_message_id == receipt.external_event_id,
    ))
    if row is None:
        return ConversationCaptureResult("SKIPPED", reason="MESSAGE_NOT_ARCHIVED")

    thread = session.get(ConversationThreadRow, row.conversation_id)
    previous = dict(row.metadata_json or {})
    if (
        thread is None
        or thread.tenant_id != receipt.tenant_id
        or thread.source != receipt.source
        or thread.source_account != details["source_account"]
        or thread.external_thread_key != details["conversation_key"]
        or row.from_me != details["from_me"]
        or row.message_type != details["message_kind"]
        or previous.get("capture_contract") != "LIVE_CONVERSATION_ARCHIVE_V0"
        or previous.get("source_inbound_event_id") != receipt.id
    ):
        raise ConversationCaptureConflict("ARTIFACT_CONVERSATION_SCOPE_CONFLICT")

    receipt_key = f"whatsapp:{receipt.external_event_id}:media"
    linked_receipt = session.scalar(select(ArtifactReceiptRow).where(
        ArtifactReceiptRow.tenant_id == receipt.tenant_id,
        ArtifactReceiptRow.source_channel == "channel.whatsapp",
        ArtifactReceiptRow.source_account == details["source_account"],
        ArtifactReceiptRow.external_receipt_id == receipt_key,
    ))
    understanding = session.scalar(select(ArtifactUnderstandingRow).where(
        ArtifactUnderstandingRow.tenant_id == receipt.tenant_id,
        ArtifactUnderstandingRow.inbound_event_id == receipt.id,
    ))
    if understanding is not None:
        if (
            understanding.content_kind not in (
                {"IMAGE"} if details["message_kind"] == "IMAGE"
                else {"IMAGE", "DOCUMENT"} if details["message_kind"] == "DOCUMENT"
                else set()
            )
            or understanding.status not in {"PENDING", "PROCESSING", "READY", "FAILED"}
        ):
            raise ConversationCaptureConflict("ARTIFACT_UNDERSTANDING_KIND_CONFLICT")

    if linked_receipt is None and understanding is None:
        return ConversationCaptureResult("PENDING", message_id=row.id)

    next_meta = dict(previous)

    def set_reference(key: str, value: str) -> None:
        old = next_meta.get(key)
        if old is not None and old != value:
            raise ConversationCaptureConflict("ARTIFACT_REFERENCE_CONFLICT")
        next_meta[key] = value

    if understanding is not None:
        set_reference("artifact_understanding_id", understanding.id)
        next_meta["artifact_understanding_status"] = understanding.status

    if linked_receipt is None:
        if understanding is None or understanding.status in {"PENDING", "PROCESSING"}:
            return ConversationCaptureResult("PENDING", message_id=row.id)
        if understanding.artifact_id or understanding.status == "READY":
            # A terminal understanding with an Artifact reference but no
            # matching source receipt is not proof of this message's media.
            raise ConversationCaptureConflict("ARTIFACT_RECEIPT_MISSING")
        next_meta["attachment_state"] = "FAILED"
    else:
        artifact = session.get(ArtifactRow, linked_receipt.artifact_id)
        if (
            artifact is None
            or artifact.tenant_id != receipt.tenant_id
            or artifact.status != "AVAILABLE"
            or artifact.artifact_kind not in {
                "VOICE": {"AUDIO"},
                "IMAGE": {"IMAGE"},
                "DOCUMENT": {"DOCUMENT", "IMAGE"},
                "VIDEO": {"VIDEO"},
            }[details["message_kind"]]
        ):
            raise ConversationCaptureConflict("ARTIFACT_KIND_OR_TENANT_CONFLICT")
        if understanding is not None and (
            understanding.artifact_id is not None
            and understanding.artifact_id != artifact.id
        ):
            raise ConversationCaptureConflict("ARTIFACT_UNDERSTANDING_LINK_CONFLICT")
        set_reference("artifact_receipt_id", linked_receipt.id)
        set_reference("artifact_id", artifact.id)
        next_meta["artifact_kind"] = artifact.artifact_kind
        next_meta["attachment_state"] = "READY"
        if details["message_kind"] in {"VOICE", "VIDEO"}:
            next_meta["artifact_understanding_status"] = "NOT_APPLICABLE"
        elif understanding is None:
            next_meta["artifact_understanding_status"] = "UNAVAILABLE"

    if next_meta == previous:
        return ConversationCaptureResult("UNCHANGED", message_id=row.id)
    row.metadata_json = next_meta
    session.flush()
    return ConversationCaptureResult("ARTIFACT_LINKED", message_id=row.id)


def _quarantine_artifact_link_conflict(
    session: Session,
    receipt: InboundEventRow,
    conflict: ConversationCaptureConflict,
) -> None:
    """Flag a source-integrity discrepancy without dropping its message.

    An impossible link should not occupy the same 25 recovery slots on every
    future poll; a separate deliberate repair can clear the quarantine.
    """
    details = _fields(receipt)
    if details is None or details["message_kind"] not in _STORED_MEDIA_KINDS:
        return
    row = session.scalar(select(ConversationMessageRow).where(
        ConversationMessageRow.tenant_id == receipt.tenant_id,
        ConversationMessageRow.source == receipt.source,
        ConversationMessageRow.source_account == details["source_account"],
        ConversationMessageRow.source_message_id == receipt.external_event_id,
    ))
    if row is None:
        return
    metadata = dict(row.metadata_json or {})
    if (
        metadata.get("capture_contract") != "LIVE_CONVERSATION_ARCHIVE_V0"
        or metadata.get("source_inbound_event_id") != receipt.id
    ):
        return
    metadata["attachment_state"] = "LINK_CONFLICT"
    # Restrict metadata to a fixed list; never store arbitrary exception
    # strings, provider names, source accounts, phone numbers or message text.
    code = str(conflict)
    metadata["artifact_link_error_code"] = (
        code if code in _MEDIA_CONFLICT_CODES else "ARTIFACT_PROVENANCE_CONFLICT"
    )
    row.metadata_json = metadata
    session.flush()


def capture_voice_transcription(
    session: Session,
    receipt: InboundEventRow,
    transcription: VoiceTranscriptionRow,
) -> ConversationCaptureResult:
    """Attach a terminal STT result to its original archived WhatsApp message.

    A transcription never creates a new conversation, never guesses the speaker,
    and never overwrites contradictory existing content.
    """
    details = _fields(receipt)
    if details is None or details["message_kind"] != "VOICE":
        return ConversationCaptureResult("SKIPPED", reason="NOT_ELIGIBLE")
    if receipt.tenant_id != transcription.tenant_id or receipt.id != transcription.inbound_event_id:
        raise ConversationCaptureConflict("TRANSCRIPTION_EVENT_SCOPE_MISMATCH")
    if transcription.status not in {"READY", "FAILED"}:
        return ConversationCaptureResult("SKIPPED", reason="NOT_TERMINAL")
    row = session.scalar(
        select(ConversationMessageRow).where(
            ConversationMessageRow.tenant_id == receipt.tenant_id,
            ConversationMessageRow.source == receipt.source,
            ConversationMessageRow.source_account == details["source_account"],
            ConversationMessageRow.source_message_id == receipt.external_event_id,
        )
    )
    if row is None:
        return ConversationCaptureResult("SKIPPED", reason="MESSAGE_NOT_ARCHIVED")
    thread = session.get(ConversationThreadRow, row.conversation_id)
    if (
        thread is None
        or thread.tenant_id != receipt.tenant_id
        or thread.external_thread_key != details["conversation_key"]
        or row.from_me != details["from_me"]
        or row.message_type != "VOICE"
    ):
        raise ConversationCaptureConflict("TRANSCRIPTION_ARCHIVE_SCOPE_MISMATCH")
    metadata = dict(row.metadata_json or {})
    if metadata.get("capture_contract") != "LIVE_CONVERSATION_ARCHIVE_V0":
        return ConversationCaptureResult("SKIPPED", reason="NOT_CAPTURED_HERE")
    if metadata.get("source_inbound_event_id") != receipt.id:
        raise ConversationCaptureConflict("TRANSCRIPTION_ARCHIVE_EVENT_MISMATCH")

    if transcription.status == "READY":
        source = (transcription.transcript_text or "").strip()
        if not source:
            raise ConversationCaptureConflict("TRANSCRIPTION_TEXT_MISSING")
        safe_text, sensitivity = _redact(source)
        if row.text is not None and row.text != safe_text:
            raise ConversationCaptureConflict("TRANSCRIPTION_CONFLICT")
        row.text = safe_text
        row.normalized_text = _normalize(safe_text)
        row.content_hash = stable_hash(safe_text)
        row.sensitivity_class = sensitivity
        row.searchable = sensitivity != "SECRET"
        metadata["content_state"] = "TRANSCRIBED"
        metadata["transcription_status"] = "READY"
    else:
        if row.text is not None:
            raise ConversationCaptureConflict("TRANSCRIPTION_STATUS_CONFLICT")
        metadata["content_state"] = "TRANSCRIPTION_FAILED"
        metadata["transcription_status"] = "FAILED"
    if transcription.completed_at:
        metadata["transcription_completed_at"] = _utc(transcription.completed_at).isoformat()
    row.metadata_json = metadata
    session.flush()
    return ConversationCaptureResult("TRANSCRIPTION_ATTACHED", message_id=row.id)


@dataclass(frozen=True)
class ConversationCaptureReconciliationResult:
    examined: int = 0
    archived: int = 0
    attached_transcripts: int = 0
    linked_artifacts: int = 0
    skipped: int = 0
    failed: int = 0


def reconcile_live_capture_batch(
    session: Session,
    *,
    limit: int = 25,
) -> ConversationCaptureReconciliationResult:
    """Recover missing *new* WhatsApp evidence after a transient ingress fault.

    Durable inbound receipts are the existing retry source. Only the explicit
    activation watermark is scanned, and only source messages absent from the
    existing archive are eligible. This never replays a decision/owner command,
    calls a model, imports history or sends anything.
    """
    if limit < 1 or limit > 50:
        raise ValueError("CONVERSATION_CAPTURE_RECONCILE_LIMIT_INVALID")
    if not settings.conversation_capture_enabled:
        return ConversationCaptureReconciliationResult()
    start = settings.conversation_capture_start_at
    if start is None or start.tzinfo is None or start.utcoffset() is None:
        return ConversationCaptureReconciliationResult()

    source_account = InboundEventRow.payload["metadata"]["source_account"].as_string()
    existing = (
        select(ConversationMessageRow.id)
        .where(
            ConversationMessageRow.tenant_id == InboundEventRow.tenant_id,
            ConversationMessageRow.source == InboundEventRow.source,
            ConversationMessageRow.source_account == source_account,
            ConversationMessageRow.source_message_id == InboundEventRow.external_event_id,
        )
        .correlate(InboundEventRow)
        .exists()
    )
    query = (
        select(InboundEventRow)
        .where(
            InboundEventRow.source == "wwebjs",
            InboundEventRow.event_type == "message",
            InboundEventRow.status == "PROCESSED",
            InboundEventRow.lineage_classification == "ORGANIC",
            InboundEventRow.received_at >= _utc(start),
            InboundEventRow.payload["event_origin"].as_string().in_(_CAPTURE_ORIGINS),
            InboundEventRow.payload["metadata"]["conversation_state"].as_string() == "READY",
            InboundEventRow.payload["metadata"]["message_type"].as_string().in_(
                _TEXT_TYPES | _VOICE_TYPES | _MEDIA_TYPES.keys()
            ),
            InboundEventRow.payload["metadata"]["conversation_key"].as_string().like("wwebjs:%"),
            ~existing,
        )
        .order_by(InboundEventRow.received_at, InboundEventRow.id)
        .limit(limit)
    )
    if settings.conversation_capture_tenant_id:
        query = query.where(
            InboundEventRow.tenant_id == settings.conversation_capture_tenant_id
        )
    receipts = session.scalars(query).all()
    archived = attached = linked = skipped = failed = 0
    for receipt in receipts:
        try:
            with session.begin_nested():
                result = capture_live_receipt(session, receipt)
        except Exception:
            # An invalid conversation identity cannot be archived. The
            # ingress event remains durable for an explicit later repair.
            failed += 1
            continue
        if result.status != "ARCHIVED":
            skipped += 1
            continue
        archived += 1
        details = _fields(receipt)
        if details is None:
            continue
        if details["message_kind"] == "VOICE":
            try:
                transcription = session.scalar(
                    select(VoiceTranscriptionRow).where(
                        VoiceTranscriptionRow.inbound_event_id == receipt.id,
                        VoiceTranscriptionRow.tenant_id == receipt.tenant_id,
                    )
                )
                if transcription is not None:
                    with session.begin_nested():
                        status = capture_voice_transcription(session, receipt, transcription)
                    attached += int(status.status == "TRANSCRIPTION_ATTACHED")
            except Exception:
                # STT failure must not erase the already archived event.
                failed += 1
        if details["message_kind"] in _STORED_MEDIA_KINDS:
            try:
                with session.begin_nested():
                    outcome = attach_canonical_artifact(session, receipt)
                linked += int(outcome.status == "ARTIFACT_LINKED")
            except ConversationCaptureConflict as exc:
                failed += 1
                try:
                    with session.begin_nested():
                        _quarantine_artifact_link_conflict(session, receipt, exc)
                except Exception:
                    failed += 1
            except Exception:
                # Media staging is independent from archive durability.
                failed += 1
    # Reconcile a *separate* failure boundary: the archive exists, STT has
    # already reached terminal state, but its text/status update was missed.
    # A missing-archive-only retry cannot repair this case. Limit both
    # queries independently so pending media cannot starve new messages.
    pending_voice_rows = session.execute(
        select(InboundEventRow, VoiceTranscriptionRow)
        .join(
            ConversationMessageRow,
            ConversationMessageRow.tenant_id == InboundEventRow.tenant_id,
        )
        .join(
            VoiceTranscriptionRow,
            VoiceTranscriptionRow.inbound_event_id == InboundEventRow.id,
        )
        .where(
            InboundEventRow.source == "wwebjs",
            InboundEventRow.received_at >= _utc(start),
            InboundEventRow.id
            == ConversationMessageRow.metadata_json["source_inbound_event_id"].as_string(),
            ConversationMessageRow.source == "wwebjs",
            ConversationMessageRow.message_type == "VOICE",
            ConversationMessageRow.metadata_json["capture_contract"].as_string()
            == "LIVE_CONVERSATION_ARCHIVE_V0",
            ConversationMessageRow.metadata_json["content_state"].as_string()
            == "PENDING_TRANSCRIPTION",
            ConversationMessageRow.tenant_id == VoiceTranscriptionRow.tenant_id,
            VoiceTranscriptionRow.tenant_id == InboundEventRow.tenant_id,
            VoiceTranscriptionRow.status.in_(("READY", "FAILED")),
        )
        .order_by(InboundEventRow.received_at, InboundEventRow.id)
        .limit(limit)
    ).all()
    for receipt, transcription in pending_voice_rows:
        try:
            with session.begin_nested():
                status = capture_voice_transcription(session, receipt, transcription)
            if status.status == "TRANSCRIPTION_ATTACHED":
                attached += 1
            else:
                skipped += 1
        except Exception:
            failed += 1

    # A different asynchronous boundary: canonical media and its semantic
    # interpretation can arrive *after* the original message was archived.
    # Only rows with a source-backed media result are eligible. Pending
    # downloads without a result must not starve later READY media.
    source_key = ConversationMessageRow.metadata_json["source_inbound_event_id"].as_string()
    link_state = ConversationMessageRow.metadata_json["attachment_state"].as_string()
    prior_understanding = ConversationMessageRow.metadata_json[
        "artifact_understanding_status"
    ].as_string()
    link_key = (
        literal("whatsapp:") + InboundEventRow.external_event_id + literal(":media")
    )
    pending_media_query = (
        select(InboundEventRow)
        .join(
            ConversationMessageRow,
            and_(
                ConversationMessageRow.tenant_id == InboundEventRow.tenant_id,
                ConversationMessageRow.source == InboundEventRow.source,
                source_key == InboundEventRow.id,
                ConversationMessageRow.metadata_json["capture_contract"].as_string()
                == "LIVE_CONVERSATION_ARCHIVE_V0",
                ConversationMessageRow.message_type.in_(_STORED_MEDIA_KINDS),
            ),
        )
        .outerjoin(
            ArtifactReceiptRow,
            and_(
                ArtifactReceiptRow.tenant_id == InboundEventRow.tenant_id,
                ArtifactReceiptRow.source_channel == "channel.whatsapp",
                ArtifactReceiptRow.source_account == ConversationMessageRow.source_account,
                ArtifactReceiptRow.external_receipt_id == link_key,
            ),
        )
        .outerjoin(
            ArtifactUnderstandingRow,
            and_(
                ArtifactUnderstandingRow.tenant_id == InboundEventRow.tenant_id,
                ArtifactUnderstandingRow.inbound_event_id == InboundEventRow.id,
            ),
        )
        .where(
            InboundEventRow.source == "wwebjs",
            InboundEventRow.event_type == "message",
            InboundEventRow.status == "PROCESSED",
            InboundEventRow.lineage_classification == "ORGANIC",
            InboundEventRow.received_at >= _utc(start),
            or_(
                and_(
                    or_(
                        link_state == "PENDING",
                        link_state.is_(None),  # pre-V1 live VOICE rows
                    ),
                    or_(
                        ArtifactReceiptRow.id.isnot(None),
                        ArtifactUnderstandingRow.status == "FAILED",
                    ),
                ),
                # A terminal failed download can be repaired only when the
                # canonical source receipt actually becomes available. It
                # must not occupy all 25 slots in every subsequent cycle.
                and_(
                    link_state == "FAILED",
                    ArtifactReceiptRow.id.isnot(None),
                ),
                and_(
                    link_state == "READY",
                    ConversationMessageRow.message_type.in_(("IMAGE", "DOCUMENT")),
                    prior_understanding.in_(
                        ("PENDING", "PROCESSING", "UNAVAILABLE", "FAILED")
                    ),
                    ArtifactUnderstandingRow.status.in_(("READY", "FAILED")),
                    prior_understanding != ArtifactUnderstandingRow.status,
                ),
            ),
        )
        .order_by(InboundEventRow.received_at, InboundEventRow.id)
        .limit(limit)
    )
    if settings.conversation_capture_tenant_id:
        pending_media_query = pending_media_query.where(
            InboundEventRow.tenant_id == settings.conversation_capture_tenant_id
        )
    pending_media = session.scalars(pending_media_query).all()
    for receipt in pending_media:
        try:
            with session.begin_nested():
                outcome = attach_canonical_artifact(session, receipt)
            if outcome.status == "ARTIFACT_LINKED":
                linked += 1
            else:
                skipped += 1
        except ConversationCaptureConflict as exc:
            # Do not retry a contradictory Artifact identity forever.
            failed += 1
            try:
                with session.begin_nested():
                    _quarantine_artifact_link_conflict(session, receipt, exc)
            except Exception:
                failed += 1
        except Exception:
            # An unexpected transient database/media failure is retryable.
            failed += 1

    return ConversationCaptureReconciliationResult(
        examined=len(receipts) + len(pending_voice_rows) + len(pending_media),
        archived=archived, attached_transcripts=attached,
        linked_artifacts=linked, skipped=skipped, failed=failed,
    )


__all__ = [
    "ConversationCaptureConflict",
    "ConversationCaptureReconciliationResult",
    "reconcile_live_capture_batch",
    "ConversationCaptureResult",
    "capture_live_receipt",
    "attach_canonical_artifact",
    "capture_voice_transcription",
]
