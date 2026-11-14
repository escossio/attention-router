"""Live-only WhatsApp conversation capture into existing PostgreSQL models.

All examples are synthetic. No actual WhatsApp, STT provider or model is called.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from attention_router.adapters.inbound import NormalizedInboundEvent
from attention_router.application import services
from attention_router.application.conversation_live_capture import (
    ConversationCaptureConflict,
    capture_live_receipt,
    capture_voice_transcription,
)
from attention_router.config import Settings, settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure import human_identity_models  # noqa: F401
from attention_router.infrastructure.models import (
    ConversationMessageRow,
    ConversationParticipantRow,
    ConversationThreadRow,
    InboundEventRow,
    MemoryCandidateRow,
    MemoryIngestionJobRow,
    QueueRow,
    VoiceTranscriptionRow,
)
from tests.test_owner_reply_grace import (
    PEER_LID,
    _inbound,
    _install_canary,
    _owner_observation,
)

STAMP = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)


@pytest.fixture()
def capture(session, monkeypatch):
    _install_canary(session)
    session.commit()
    monkeypatch.setattr(settings, "conversation_capture_enabled", True)
    monkeypatch.setattr(settings, "conversation_capture_start_at", STAMP - timedelta(minutes=1))
    monkeypatch.setattr(settings, "conversation_capture_tenant_id", DEFAULT_TENANT_ID)
    monkeypatch.setattr(settings, "persistent_memory_enabled", False)
    monkeypatch.setattr(settings, "memory_ingestion_enabled", False)
    return session


def inbound(uid, text, *, at=STAMP, metadata=None) -> NormalizedInboundEvent:
    original = _inbound(uid, at)
    return original.model_copy(update={
        "content": text,
        "metadata": {
            **original.metadata,
            "message_type": "chat",
            "has_media": False,
            **(metadata or {}),
        },
    })


def outgoing(uid, text, *, at=STAMP, origin="OWNER_MANUAL_OUTBOUND_OBSERVED"):
    original = _owner_observation(uid, at, classification=origin)
    return original.model_copy(update={
        "content": text,
        "metadata": {**original.metadata, "message_type": "chat"},
    })


def messages(session):
    return session.scalars(select(ConversationMessageRow).order_by(ConversationMessageRow.sent_at)).all()


def _receipt(session, event: NormalizedInboundEvent) -> InboundEventRow:
    return session.scalar(select(InboundEventRow).where(
        InboundEventRow.tenant_id == event.tenant_id,
        InboundEventRow.source == event.source,
        InboundEventRow.external_event_id == event.external_event_id,
    ))


def _transcription(session, event, *, status="READY", text="voz sintetica"):
    record = _receipt(session, event)
    assert record is not None
    stamp = now_utc()
    row = VoiceTranscriptionRow(
        id=new_id(),
        tenant_id=event.tenant_id,
        inbound_event_id=record.id,
        media_artifact_id=None,
        status=status,
        transcript_text=text if status == "READY" else None,
        provider="synthetic-stt",
        model="synthetic",
        provider_request_reference=None,
        error_code="FAILED_SYNTHETIC" if status == "FAILED" else None,
        attempt_count=1,
        claimed_at=None,
        claimed_by=None,
        created_at=stamp,
        updated_at=stamp,
        completed_at=stamp,
    )
    session.add(row)
    session.flush()
    return record, row


def test_feature_disabled_never_captures_or_extracts(session, monkeypatch):
    _install_canary(session)
    session.commit()
    monkeypatch.setattr(settings, "conversation_capture_enabled", False)
    result = services.receive_normalized_inbound_event(
        session, inbound("cap-off", "Bom dia"),
    )
    session.commit()
    assert result
    assert messages(session) == []
    assert session.scalar(select(func.count()).select_from(MemoryIngestionJobRow)) == 0


def test_live_text_bidirectional_same_conversation_idempotent_no_memory_jobs(capture):
    rx = inbound("cap-in-text", "Pretão é um gato.", at=STAMP)
    tx = outgoing("cap-owner-text", "Ele não está bem.", at=STAMP+timedelta(seconds=2))
    first = services.receive_normalized_inbound_event(capture, rx)
    capture.commit()
    before_queue = capture.scalar(select(func.count()).select_from(QueueRow))
    sent = services.receive_normalized_inbound_event(capture, tx)
    capture.commit()
    assert sent["interaction_id"] is None
    assert capture.scalar(select(func.count()).select_from(QueueRow)) == before_queue
    records = messages(capture)
    assert len(records) == 2
    assert records[0].direction == "INBOUND" and records[0].from_me is False
    assert records[1].direction == "OUTBOUND" and records[1].from_me is True
    assert records[0].text == "Pretão é um gato."
    assert records[1].text == "Ele não está bem."
    assert records[0].conversation_id == records[1].conversation_id
    assert records[0].metadata_json["speaker_basis"] == "EXTERNAL_SENDER"
    assert records[1].metadata_json["event_origin"] == "OWNER_MANUAL_OUTBOUND_OBSERVED"
    assert records[1].external_sender_key != PEER_LID  # never call a peer the owner
    assert records[0].metadata_json["capture_contract"] == "LIVE_CONVERSATION_ARCHIVE_V0"
    # SQLite fixture strips tzinfo; the PostgreSQL contract remains timestamptz.
    assert records[0].sent_at.replace(tzinfo=UTC) == STAMP
    assert records[1].metadata_json["timestamp_basis"] == "PROVIDER_OCCURRED_AT"
    assert records[0].metadata_json["source_received_at"]
    assert capture.scalar(select(func.count()).select_from(ConversationThreadRow)) == 1
    assert capture.scalar(select(func.count()).select_from(ConversationParticipantRow)) >= 1
    assert capture.scalar(select(func.count()).select_from(MemoryCandidateRow)) == 0
    assert capture.scalar(select(func.count()).select_from(MemoryIngestionJobRow)) == 0
    second = services.receive_normalized_inbound_event(capture, rx)
    capture.commit()
    assert second
    assert len(messages(capture)) == 2
    assert first["id"]


def test_owner_observation_without_text_is_never_fabricated(capture):
    event = outgoing("owner-empty-text", "")
    services.receive_normalized_inbound_event(capture, event)
    capture.commit()
    row = messages(capture)[0]
    assert row.text is None
    assert row.metadata_json["content_state"] == "TEXT_UNAVAILABLE"
    assert row.direction == "OUTBOUND"
    assert row.external_sender_key != PEER_LID


def test_unknown_from_me_is_preserved_without_attributing_owner(capture):
    event = outgoing("unknown-outbound", "Texto observado", origin="UNKNOWN_FROM_ME")
    services.receive_normalized_inbound_event(capture, event)
    capture.commit()
    row = messages(capture)[0]
    assert row.text == "Texto observado"
    assert row.from_me is True
    assert row.external_sender_key is None
    assert row.sender_actor_id is None
    assert row.metadata_json["speaker_basis"] == "UNRESOLVED"
    assert row.metadata_json["event_origin"] == "UNKNOWN_FROM_ME"


def test_router_output_observed_without_claiming_human_sender(capture):
    event = outgoing(
        "andy-outbound", "Mensagem automatizada",
        origin="ROUTER_AUTOMATED_OUTBOUND_OBSERVED",
    )
    services.receive_normalized_inbound_event(capture, event)
    capture.commit()
    row = messages(capture)[0]
    assert row.text == "Mensagem automatizada"
    assert row.external_sender_key is None
    assert row.metadata_json["event_origin"] == "ROUTER_AUTOMATED_OUTBOUND_OBSERVED"


def test_voice_pending_then_ready_transcript_attaches_to_same_source_message(capture):
    event = inbound(
        "cap-voice",
        "",
        metadata={"message_type": "ptt", "has_media": True},
    )
    services.receive_normalized_inbound_event(capture, event)
    capture.commit()
    original = messages(capture)[0]
    original_id, original_thread = original.id, original.conversation_id
    assert original.message_type == "VOICE" and original.text is None
    assert original.metadata_json["content_state"] == "PENDING_TRANSCRIPTION"
    receipt, stt = _transcription(capture, event, text="Pretão é um gato.")
    attached = capture_voice_transcription(capture, receipt, stt)
    capture.commit()
    assert attached.status == "TRANSCRIPTION_ATTACHED"
    capture.refresh(original)
    assert original.text == "Pretão é um gato."
    assert original.metadata_json["transcription_status"] == "READY"
    assert original.metadata_json["transcription_completed_at"]
    assert original.id == original_id and original.conversation_id == original_thread
    assert len(messages(capture)) == 1
    assert capture_voice_transcription(capture, receipt, stt).status == "TRANSCRIPTION_ATTACHED"
    capture.commit()
    assert len(messages(capture)) == 1


def test_voice_terminal_failure_keeps_message_and_provenance(capture):
    event = inbound("cap-failed-voice", "", metadata={"message_type":"audio","has_media":True})
    services.receive_normalized_inbound_event(capture, event)
    capture.commit()
    receipt, stt = _transcription(capture, event, status="FAILED")
    result=capture_voice_transcription(capture, receipt, stt)
    capture.commit()
    row=messages(capture)[0]
    assert result.status=="TRANSCRIPTION_ATTACHED"
    assert row.text is None
    assert row.metadata_json["transcription_status"] == "FAILED"
    assert row.metadata_json["content_state"] == "TRANSCRIPTION_FAILED"


def test_voice_tenant_mismatch_does_not_attach(capture):
    event = inbound("cap-stt-scope", "", metadata={"message_type": "ptt", "has_media": True})
    services.receive_normalized_inbound_event(capture, event)
    receipt, stt = _transcription(capture, event)
    stt.tenant_id = "foreign-tenant"
    with pytest.raises(ConversationCaptureConflict, match="SCOPE_MISMATCH"):
        capture_voice_transcription(capture, receipt, stt)


def test_voice_cannot_replace_preexisting_transcript(capture):
    event = inbound("cap-stt-immutability", "", metadata={"message_type":"ptt","has_media":True})
    services.receive_normalized_inbound_event(capture, event)
    receipt,stt=_transcription(capture,event,text="Texto verdadeiro")
    capture_voice_transcription(capture,receipt,stt)
    capture.commit()
    stt.transcript_text="Outro conteúdo conflitante"
    with pytest.raises(ConversationCaptureConflict,match="TRANSCRIPTION_CONFLICT"):
        capture_voice_transcription(capture,receipt,stt)
    capture.rollback()


def test_unverified_conversation_or_old_events_are_not_archived(capture):
    tests=[
        inbound("not-ready","Texto",metadata={"conversation_state":"AMBIGUOUS"}),
        inbound("old-event","Texto",at=STAMP-timedelta(days=1)),
        inbound("not-whatsapp","Texto").model_copy(update={"channel":"gmail"}),
        inbound("fake-actor","Texto").model_copy(update={"lineage_classification":"HISTORICAL_UNKNOWN"}),
        inbound("wrong-tenant","Texto").model_copy(update={"tenant_id":"other"}),
        inbound("image-message","Texto",metadata={"message_type":"image"}),
        inbound("wrong-fromme","Texto",metadata={"from_me":True}),
    ]
    for event in tests:
        # Test the snapshot contract, not the source gate side effects.
        receipt = InboundEventRow(
            id=new_id(),tenant_id=event.tenant_id,source=event.source,
            external_event_id=event.external_event_id,
            event_type=event.event_type,payload=event.normalized_payload,
            payload_hash="synthetic",received_at=event.received_at,
            processed_at=None,interaction_id=None,status="PROCESSED",
            error=None,correlation_id=new_id(),
            lineage_classification=event.lineage_classification,
            scenario_run_id=None,scenario_step_run_id=None,
        )
        assert capture_live_receipt(capture,receipt).status=="SKIPPED"
    assert messages(capture)==[]


def test_owner_command_never_enters_conversation_archive(capture):
    event=outgoing("owner-command", "retomar").model_copy(update={
        "event_origin":"OWNER_COMMAND",
        "owner_authenticated":True,
        "metadata":{"conversation_key":"wwebjs:"+PEER_LID,
                    "conversation_state":"READY","from_me":True,"message_type":"chat"},
    })
    receipt = InboundEventRow(
        id=new_id(),tenant_id=event.tenant_id,source=event.source,
        external_event_id=event.external_event_id,event_type="message",
        payload=event.normalized_payload,payload_hash="synthetic",
        received_at=event.received_at,status="PROCESSED",
        lineage_classification="ORGANIC",correlation_id=new_id(),
    )
    assert capture_live_receipt(capture,receipt).status=="SKIPPED"
    assert messages(capture)==[]


def test_activated_capture_requires_explicit_aware_start_timestamp():
    common = {
        "_env_file":None,
        "app_env":"test",
        "internal_ingress_hmac_secret":"unit-test-internal-ingress-secret-32-bytes",
        "conversation_capture_enabled":True,
    }
    with pytest.raises(ValueError,match="CONVERSATION_CAPTURE_START_AT_REQUIRED"):
        Settings(**common)
    with pytest.raises(ValueError,match="CONVERSATION_CAPTURE_START_AT_REQUIRED"):
        Settings(**common,conversation_capture_start_at=datetime(2026,11,14))
    valid=Settings(**common,conversation_capture_start_at=STAMP)
    assert valid.conversation_capture_enabled is True
    assert valid.conversation_capture_start_at==STAMP


def test_reconciliation_recovers_only_new_missing_receipts(capture, monkeypatch):
    from attention_router.application.conversation_live_capture import (
        reconcile_live_capture_batch,
    )
    event = inbound("recover-missed", "Mensagem persistida, arquivo ainda ausente")
    monkeypatch.setattr(settings, "conversation_capture_enabled", False)
    services.receive_normalized_inbound_event(capture, event)
    capture.commit()
    assert messages(capture) == []
    monkeypatch.setattr(settings, "conversation_capture_enabled", True)
    result = reconcile_live_capture_batch(capture)
    capture.commit()
    assert result.archived == 1
    assert result.examined == 1
    assert len(messages(capture)) == 1
    assert messages(capture)[0].text == "Mensagem persistida, arquivo ainda ausente"
    assert reconcile_live_capture_batch(capture).examined == 0


def test_reconciliation_repairs_voice_when_transcript_finished_before_archive(
    capture, monkeypatch,
):
    from attention_router.application.conversation_live_capture import (
        reconcile_live_capture_batch,
    )
    event = inbound(
        "recover-voice", "", metadata={"message_type": "ptt", "has_media": True},
    )
    monkeypatch.setattr(settings, "conversation_capture_enabled", False)
    services.receive_normalized_inbound_event(capture, event)
    receipt, _stt = _transcription(capture, event, text="A voz estava registrada.")
    capture.commit()
    monkeypatch.setattr(settings, "conversation_capture_enabled", True)
    result = reconcile_live_capture_batch(capture)
    capture.commit()
    assert result.examined == 1
    assert result.archived == 1
    assert result.attached_transcripts == 1
    assert messages(capture)[0].metadata_json["source_inbound_event_id"] == receipt.id
    assert messages(capture)[0].text == "A voz estava registrada."
    assert messages(capture)[0].metadata_json["transcription_status"] == "READY"
    assert reconcile_live_capture_batch(capture).examined == 0


def test_reconciliation_never_imports_older_events(capture, monkeypatch):
    from attention_router.application.conversation_live_capture import (
        reconcile_live_capture_batch,
    )
    event = inbound("reconcile-old", "Texto de antes", at=STAMP - timedelta(days=7))
    monkeypatch.setattr(settings, "conversation_capture_enabled", False)
    services.receive_normalized_inbound_event(capture, event)
    capture.commit()
    monkeypatch.setattr(settings, "conversation_capture_enabled", True)
    report = reconcile_live_capture_batch(capture)
    assert report.examined == 0
    assert messages(capture) == []


def test_worker_stt_callback_attaches_transcript_without_reply(
    session, monkeypatch, tmp_path,
):
    from types import SimpleNamespace
    from attention_router.application.voice_transcription import process_voice_transcriptions
    from tests.test_whatsapp_voice_flow import (
        setup_direct, voice_event, attach_media,
    )
    from attention_router.infrastructure.models import OutboxMessageRow

    setup_direct(session, monkeypatch)
    monkeypatch.setattr(settings, "conversation_capture_enabled", True)
    monkeypatch.setattr(
        settings, "conversation_capture_start_at",
        now_utc() - timedelta(minutes=2),
    )
    monkeypatch.setattr(settings, "conversation_capture_tenant_id", DEFAULT_TENANT_ID)
    monkeypatch.setattr(settings, "persistent_memory_enabled", False)
    monkeypatch.setattr(settings, "memory_ingestion_enabled", False)
    event = voice_event()
    received = services.receive_normalized_inbound_event(session, event)
    session.commit()
    assert len(messages(session)) == 1
    assert messages(session)[0].text is None
    attach_media(session, monkeypatch, tmp_path, {
        **received,
        "external_event_id": event.external_event_id,
    })
    outbox_before = session.scalar(select(func.count()).select_from(OutboxMessageRow))
    client = SimpleNamespace(transcribe=lambda *_: {
        "transcript": "Pretão é um gato.",
        "provider": "synthetic",
        "model": "synthetic",
        "request_id": "opaque-test",
    })
    assert process_voice_transcriptions(session, "synthetic-stt", client=client) == 1
    session.commit()
    row = messages(session)[0]
    assert row.text == "Pretão é um gato."
    assert row.metadata_json["transcription_status"] == "READY"
    assert session.scalar(select(func.count()).select_from(OutboxMessageRow)) == outbox_before
    assert session.scalar(select(func.count()).select_from(MemoryIngestionJobRow)) == 0
    assert session.scalar(select(func.count()).select_from(MemoryCandidateRow)) == 0


def test_manually_sent_voice_enters_existing_media_stt_and_archives_without_replies(
    capture, monkeypatch, tmp_path,
):
    from types import SimpleNamespace
    from attention_router.application.voice_media import (
        MediaReadyNotification,
        record_media_notification,
    )
    from attention_router.application.voice_transcription import (
        process_voice_transcriptions,
    )
    from attention_router.infrastructure.media_store import MediaStore
    from attention_router.infrastructure.models import OutboxMessageRow

    event = outgoing(
        "synthetic-owner-voice",
        "",
        origin="OWNER_MANUAL_OUTBOUND_OBSERVED",
    ).model_copy(update={
        "metadata": {
            **outgoing("synthetic-owner-voice", "").metadata,
            "message_type": "ptt",
            "has_media": True,
        }
    })
    capture_result = services.receive_normalized_inbound_event(capture, event)
    capture.commit()
    assert capture_result["interaction_id"] is None
    original = messages(capture)[0]
    assert original.direction == "OUTBOUND"
    assert original.message_type == "VOICE"
    assert original.text is None
    assert original.metadata_json["content_state"] == "PENDING_TRANSCRIPTION"

    monkeypatch.setattr(settings, "whatsapp_media_root", str(tmp_path))
    content = b"OggS synthetic outgoing voice"
    ref, digest, size, _ = MediaStore(tmp_path, 1024 * 1024).put_bytes(
        content, mime_type="audio/ogg"
    )
    ready = record_media_notification(
        capture,
        MediaReadyNotification(
            tenant_id=DEFAULT_TENANT_ID,
            source="wwebjs",
            external_event_id=event.external_event_id,
            media_ref=ref,
            content_sha256=digest,
            mime_type="audio/ogg",
            size_bytes=size,
            media_kind="ptt",
            capture_status="READY",
        ),
    )
    assert ready is not None and ready.purpose == "VOICE_INPUT"
    capture.commit()
    transcriber = SimpleNamespace(transcribe=lambda _path, _mime: {
        "transcript": "O Pretão está doente.",
        "provider": "synthetic",
        "model": "synthetic",
        "request_id": "opaque-synthetic",
    })
    assert process_voice_transcriptions(capture, "synthetic-stt", client=transcriber) == 1
    capture.commit()
    capture.refresh(original)
    assert original.text == "O Pretão está doente."
    assert original.metadata_json["transcription_status"] == "READY"
    assert original.id == messages(capture)[0].id
    assert capture.scalar(select(func.count()).select_from(OutboxMessageRow)) == 0
    assert capture.scalar(select(func.count()).select_from(MemoryIngestionJobRow)) == 0
