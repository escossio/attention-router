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


def test_reconciliation_repairs_terminal_stt_missing_from_existing_voice_record(capture):
    from attention_router.application.conversation_live_capture import (
        reconcile_live_capture_batch,
    )
    event = inbound(
        "synthetic-stt-attachment-missed",
        "",
        metadata={"message_type": "ptt", "has_media": True},
    )
    services.receive_normalized_inbound_event(capture, event)
    receipt, stt = _transcription(capture, event, text="Pretão é um gato.")
    capture.commit()

    original = messages(capture)[0]
    assert original.text is None
    original_id = original.id
    assert original.metadata_json["content_state"] == "PENDING_TRANSCRIPTION"

    # Model a transient commit/attach failure after STT reached READY.
    recovered = reconcile_live_capture_batch(capture, limit=10)
    capture.commit()
    assert recovered.archived == 0
    assert recovered.attached_transcripts == 1
    assert recovered.failed == 0
    assert recovered.examined == 1
    capture.refresh(original)
    assert original.id == original_id
    assert original.text == "Pretão é um gato."
    assert original.metadata_json["transcription_status"] == "READY"
    assert original.metadata_json["source_inbound_event_id"] == receipt.id
    assert len(messages(capture)) == 1
    assert stt.status == "READY"
    assert reconcile_live_capture_batch(capture).examined == 0


def test_reconciliation_ignores_unfinished_voice_transcription(capture):
    from attention_router.application.conversation_live_capture import (
        reconcile_live_capture_batch,
    )
    event = inbound(
        "synthetic-stt-still-working",
        "",
        at=now_utc(),  # This test covers in-flight STT before the capture deadline.
        metadata={"message_type": "ptt", "has_media": True},
    )
    services.receive_normalized_inbound_event(capture, event)
    record = _receipt(capture, event)
    assert record is not None
    stamp = now_utc()
    capture.add(VoiceTranscriptionRow(
        id=new_id(), tenant_id=event.tenant_id, inbound_event_id=record.id,
        media_artifact_id=None, status="PENDING", transcript_text=None,
        provider=None, model=None, provider_request_reference=None,
        error_code=None, attempt_count=0, claimed_at=None, claimed_by=None,
        created_at=stamp, updated_at=stamp, completed_at=None,
    ))
    capture.commit()
    result = reconcile_live_capture_batch(capture, limit=10)
    assert result.examined == 0
    assert messages(capture)[0].metadata_json["content_state"] == "PENDING_TRANSCRIPTION"


def test_policy_voice_transcript_preserves_raw_origin_and_preview(capture):
    from attention_router.application.conversation_preview import preview_conversation
    event = outgoing("policy-voice", "", origin="UNKNOWN_FROM_ME")
    event = event.model_copy(update={"metadata": {
        **event.metadata, "message_type": "ptt", "has_media": True,
        "final_from_me_classification": "UNKNOWN_FROM_ME",
        "from_me_classification_reason": "UNMATCHED_MEDIA_FROM_ME",
        "semantic_attribution": {"basis": "OWNER_ACCOUNT_ATTRIBUTED_BY_POLICY_V1",
                                 "tenant_id": event.tenant_id,
                                 "source_account": event.metadata["source_account"],
                                 "account_verified": True},
    }})
    services.receive_normalized_inbound_event(capture, event)
    capture.commit()
    row = messages(capture)[0]
    original_id = row.id
    assert row.metadata_json["event_origin"] == "UNKNOWN_FROM_ME"
    record, transcript = _transcription(capture, event, text="Encontro sintético amanhã.")
    capture_voice_transcription(capture, record, transcript)
    capture_voice_transcription(capture, record, transcript)
    assert messages(capture)[0].id == original_id
    assert row.metadata_json["speaker_basis"] == "OWNER_ACCOUNT_ATTRIBUTED_BY_POLICY_V1"
    assert row.metadata_json["transcription_id"] == transcript.id
    assert row.sent_at.replace(tzinfo=UTC) == STAMP
    preview = preview_conversation(capture, tenant_id=event.tenant_id,
        source_account=event.metadata["source_account"],
        conversation_key=event.metadata["conversation_key"])
    assert preview["status"] == "PREVIEW_ONLY"
    assert preview["messages"][0]["text"] == "Encontro sintético amanhã."
    assert preview["durable_claims"] == []
    assert preview_conversation(capture, tenant_id="other-tenant",
        source_account=event.metadata["source_account"],
        conversation_key=event.metadata["conversation_key"])["status"] == "NOT_FOUND"


def test_proved_alias_resolves_lid_and_phone_without_cross_tenant(capture):
    from attention_router.application.conversation_preview import resolve_conversation_alias
    phone = "5500000000025@c.us"
    event = inbound("alias-proof", "Mensagem sintética", metadata={
        "peer_identifiers": [PEER_LID, phone], "alias_provenance": "PROVIDER_LID_PN_MAPPING",
    })
    services.receive_normalized_inbound_event(capture, event)
    capture.commit()
    args = dict(tenant_id=event.tenant_id, source_account=event.metadata["source_account"])
    a = resolve_conversation_alias(capture, **args, identifier=phone)
    b = resolve_conversation_alias(capture, **args, identifier=PEER_LID)
    assert a == b and a["status"] == "MATCH"
    assert resolve_conversation_alias(capture, tenant_id="other",
        source_account=args["source_account"], identifier=phone)["status"] == "NOT_FOUND"


def test_synthetic_bilateral_preview_incomplete_and_no_claims(capture):
    from attention_router.application.conversation_preview import preview_selected_conversation
    first = inbound("demo-in", "O encontro sintético será amanhã.")
    second = outgoing("demo-out", "Confirmo o encontro sintético.", at=STAMP+timedelta(seconds=2))
    third = outgoing("demo-audio", "", at=STAMP+timedelta(seconds=3), origin="UNKNOWN_FROM_ME")
    third = third.model_copy(update={"metadata": {**third.metadata, "message_type": "ptt"}})
    for event in (first, second, third):
        services.receive_normalized_inbound_event(capture, event)
    capture.commit()
    result = preview_selected_conversation(capture, tenant_id=first.tenant_id,
        source_account=first.metadata["source_account"], identifier=PEER_LID)
    assert [m["provider_id"] for m in result["messages"]] == [e.external_event_id for e in (first, second, third)]
    assert result["context_incomplete"] is True
    assert result["messages"][2]["text"] is None
    assert result["messages"][2]["author_basis"] == "UNRESOLVED"
    assert len(result["episodes"]) == 1
    assert len(result["episodes"][0]["message_ids"]) == 3
    assert capture.scalar(select(func.count()).select_from(MemoryIngestionJobRow)) == 0


@pytest.mark.parametrize("capture_state", [None, "TRANSCRIPTION_PENDING"])
def test_deadline_preserves_message_and_late_transcription_recovers(capture, capture_state):
    from attention_router.application.conversation_live_capture import expire_pending_capture
    event = inbound('deadline-voice', '', metadata={'message_type':'ptt', 'has_media':True,
                                                  'media_capture_status':capture_state})
    services.receive_normalized_inbound_event(capture, event)
    capture.commit()
    row = messages(capture)[0]
    assert expire_pending_capture(capture, now=STAMP+timedelta(minutes=16)) == 1
    assert row.metadata_json['content_state'] == 'MEDIA_CAPTURE_TIMEOUT'
    assert row.text is None
    receipt, transcription = _transcription(capture, event, text='Recuperação sintética')
    capture_voice_transcription(capture, receipt, transcription)
    assert row.metadata_json['content_state'] == 'TRANSCRIBED'


def test_alias_review_keeps_provider_evidence_and_is_reversible(capture):
    from attention_router.application.conversation_preview import resolve_conversation_alias, set_alias_validity
    phone = '5500000000025@c.us'
    event = inbound('alias-review', 'Texto sintético', metadata={
        'peer_identifiers':[PEER_LID,phone], 'alias_provenance':'PROVIDER_LID_PN_MAPPING'})
    services.receive_normalized_inbound_event(capture,event)
    capture.commit()
    row=messages(capture)[0]
    scope={'tenant_id':event.tenant_id,'source_account':event.metadata['source_account']}
    set_alias_validity(capture,**scope,message_id=row.id,active=False,decision_ref='synthetic-revoke')
    assert resolve_conversation_alias(capture,**scope,identifier=phone)['status']=='NOT_FOUND'
    assert row.metadata_json['provider_peer_aliases']==[PEER_LID,phone]
    set_alias_validity(capture,**scope,message_id=row.id,active=True,decision_ref='synthetic-restore')
    assert resolve_conversation_alias(capture,**scope,identifier=phone)['status']=='MATCH'
    assert len(row.metadata_json['alias_decisions'])==2


@pytest.mark.parametrize('mutation', [
    {'from_me_classification_reason':'MEDIA_REFERENCE_PENDING'},
    {'is_group':True}, {'owner_self_chat':True},
    {'outbound_provenance':{'outbox_id':'synthetic'}},
    {'semantic_attribution':{'basis':'OWNER_ACCOUNT_ATTRIBUTED_BY_POLICY_V1','account_verified':True,
                             'tenant_id':'different-tenant','source_account':'default'}},
])
def test_policy_voice_denies_ambiguous_or_cross_scope_author(capture, mutation):
    event=outgoing('policy-denied','',origin='UNKNOWN_FROM_ME')
    meta={**event.metadata,'message_type':'ptt','has_media':True,
          'final_from_me_classification':'UNKNOWN_FROM_ME',
          'from_me_classification_reason':'UNMATCHED_MEDIA_FROM_ME',
          'semantic_attribution':{'basis':'OWNER_ACCOUNT_ATTRIBUTED_BY_POLICY_V1','account_verified':True,
                                  'tenant_id':event.tenant_id,'source_account':event.metadata['source_account']}}
    meta.update(mutation)
    event=event.model_copy(update={'metadata':meta})
    services.receive_normalized_inbound_event(capture,event)
    capture.commit()
    row=messages(capture)[0]
    assert row.metadata_json['speaker_basis']=='UNRESOLVED'
    assert row.metadata_json['event_origin']=='UNKNOWN_FROM_ME'


def synthetic_event_messages():
    rows = [
        ('m10', 'Maria', 'maria', '2026-11-10T12:00:00+00:00', 'Proponho viagem sábado [evento:aurora].'),
        ('o12', 'OWNER', 'maria', '2026-11-12T12:00:00+00:00', 'Remarco a viagem para domingo às 09:00 [evento:aurora].'),
        ('m13', 'Maria', 'maria', '2026-11-13T12:00:00+00:00', 'Confirmo domingo às 09:00 [evento:aurora].'),
        ('j13', 'Joao', 'joao', '2026-11-13T13:00:00+00:00', 'Estou ciente da mudança [evento:aurora].'),
        ('p13', 'Pedro', 'pedro', '2026-11-13T14:00:00+00:00', 'Menciono a viagem [evento:aurora].'),
        ('other', 'Pedro', 'pedro', '2026-11-13T15:00:00+00:00', 'Proponho viagem domingo [evento:outra].'),
        ('uncertain', 'Joao', 'joao', '2026-11-13T16:00:00+00:00', 'Proponho passeio amanhã.'),
    ]
    return [dict(message_id=mid, author=author, conversation_key=conversation,
                 sent_at=sent, text=text, tenant_id='synthetic', source_account='account',
                 author_basis='OWNER_ACCOUNT_ATTRIBUTED_BY_POLICY_V1' if author == 'OWNER' else 'EXTERNAL_SENDER',
                 origin='UNKNOWN_FROM_ME' if author == 'OWNER' else 'EXTERNAL_INBOUND',
                 kind='VOICE' if mid in {'m10', 'o12'} else 'TEXT',
                 evidence={'transcription_id': 'synthetic-stt-' + mid} if mid in {'m10', 'o12'} else {})
            for mid, author, conversation, sent, text in rows]


def test_multiday_cross_conversation_event_preview(tmp_path):
    import json
    from attention_router.application.conversation_preview import correlate_preview_events, query_preview_events
    scope = dict(tenant_id='synthetic', source_account='account')
    messages = synthetic_event_messages()
    view = correlate_preview_events(messages[::-1], **scope)
    event = view['events'][0]
    assert event['event_id'] == 'aurora'
    assert event['current']['event_time']['date'] == '2026-11-15'
    assert event['current']['author'] == 'Maria'
    assert event['history'][0]['event_time']['date'] == '2026-11-14'
    assert event['history'][1]['action'] == 'CHANGES'
    assert event['current']['state'] == 'CONFIRMED_DECLARATION'
    assert len(view['events']) == 3
    assert view['durable_claims'] == []
    queries = {
        'planning': dict(event_id='aurora'),
        'who_confirmed': dict(event_id='aurora', action='CONFIRMS'),
        'when_changed': dict(event_id='aurora', action='CHANGES'),
        'maria_history': dict(person='Maria'),
        'joao_knows': dict(person='Joao', event_id='aurora', action='ACKNOWLEDGES'),
        'pedro_knows': dict(person='Pedro', event_id='aurora', action='ACKNOWLEDGES'),
        'meaning': dict(topic='excursão'),
        'original': dict(evidence_id='m10'),
    }
    answers = {k: query_preview_events(view, **scope, **q) for k, q in queries.items()}
    assert answers['pedro_knows']['status'] == 'NO_EVIDENCE'
    assert answers['who_confirmed']['results'][0]['matches'][0]['author'] == 'Maria'
    assert answers['joao_knows']['results'][0]['matches'][0]['message_id'] == 'j13'
    assert len(answers['meaning']['results']) == 3
    (tmp_path / 'events-demo.json').write_text(json.dumps({'preview': view, 'queries': answers}, ensure_ascii=False, indent=2))


@pytest.mark.parametrize('text,stamp,zone,date,basis', [
    ('amanhã', '2026-11-12T01:00:00+00:00', 'America/Sao_Paulo', '2026-11-12', 'RELATIVE_TO_PROVIDER_TIME'),
    ('amanhã', '2026-11-12T01:00:00+00:00', 'UTC', '2026-11-13', 'RELATIVE_TO_PROVIDER_TIME'),
    ('domingo', '2026-11-13T12:00:00+00:00', 'UTC', '2026-11-15', 'NEXT_WEEKDAY_HYPOTHESIS'),
    ('2026-11-22 às 15:30', '2026-11-13T12:00:00+00:00', 'UTC', '2026-11-22', 'EXPLICIT_DATE'),
])
def test_event_time_uses_provider_clock(text, stamp, zone, date, basis):
    from attention_router.application.conversation_preview import preview_event_time
    result = preview_event_time(text, stamp, zone)
    assert result['date'] == date and result['basis'] == basis
    assert result['occurred'] is False


def test_event_scope_reviews_conflicts_and_no_automated_claims():
    from attention_router.application.conversation_preview import correlate_preview_events, query_preview_events
    scope = dict(tenant_id='synthetic', source_account='account')
    messages = synthetic_event_messages()
    with pytest.raises(ValueError, match='SCOPE'):
        correlate_preview_events(messages, tenant_id='other', source_account='account')
    review = dict(**scope, decision_ref='r1', message_id='m13', action='REVOKE')
    view = correlate_preview_events(messages, **scope, reviews=[review, review])
    assert view['events'][0]['current']['state'] == 'PROPOSED'
    assert 'm13' in view['evidence']  # original archive is never rewritten
    restored = {**review, 'decision_ref': 'r2', 'action': 'RESTORE'}
    assert correlate_preview_events(messages, **scope, reviews=[review, restored])['events'][0]['current']['state'] == 'CONFIRMED_DECLARATION'
    messages.append({**messages[2], 'message_id': 'contradiction', 'sent_at': '2026-11-14T12:00:00+00:00',
                     'text': 'Confirmo 2026-11-16 [evento:aurora].'})
    assert correlate_preview_events(messages, **scope)['events'][0]['current']['state'] == 'CONFLICT'
    messages[-1]['origin'] = 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED'
    view = correlate_preview_events(messages, **scope)
    assert view['events'][0]['current']['state'] == 'CONFIRMED_DECLARATION'
    assert view['context_incomplete']
    with pytest.raises(ValueError, match='SCOPE'):
        query_preview_events(view, tenant_id='other', source_account='account')


def test_event_ambiguous_dates_and_quote_continuity():
    from attention_router.application.conversation_preview import correlate_preview_events, preview_event_time
    assert preview_event_time('sábado ou domingo', STAMP.isoformat(), 'UTC')['status'] == 'AMBIGUOUS'
    assert preview_event_time('2026-02-30', STAMP.isoformat(), 'UTC')['status'] == 'INVALID_DATE'
    rows = synthetic_event_messages()[:2]
    rows[1]['text'] = 'Remarco para domingo.'
    rows[1]['reply_message_id'] = 'm10'
    view = correlate_preview_events(rows, tenant_id='synthetic', source_account='account')
    assert len(view['events']) == 1
    assert view['events'][0]['current']['event_time']['date'] == '2026-11-15'


def test_event_search_interval_and_unconfirmed_mentions():
    from attention_router.application.conversation_preview import correlate_preview_events, query_preview_events
    scope = dict(tenant_id='synthetic', source_account='account')
    rows = synthetic_event_messages()
    rows.append({**rows[2], 'message_id': 'reported', 'sent_at': '2026-11-14T12:00:00+00:00',
                 'text': 'Pedro disse confirmo segunda [evento:aurora].'})
    view = correlate_preview_events(rows, **scope)
    assert view['events'][0]['current']['message_id'] == 'm13'
    result = query_preview_events(view, **scope, event_id='aurora',
                                 since='2026-11-12T00:00:00', until='2026-11-12T23:59:59+00:00')
    assert [h['message_id'] for h in result['results'][0]['matches']] == ['o12']
    rows.append({**rows[2], 'message_id': 'hour', 'sent_at': '2026-11-14T13:00:00+00:00',
                 'text': 'Confirmo domingo às 10:00 [evento:aurora].'})
    assert correlate_preview_events(rows, **scope)['events'][0]['current']['state'] == 'CONFLICT'


def test_contestation_without_date_does_not_silently_confirm():
    from attention_router.application.conversation_preview import correlate_preview_events
    rows = synthetic_event_messages()
    rows.append({**rows[2], 'message_id': 'contested', 'sent_at': '2026-11-14T12:00:00+00:00',
                 'text': 'Discordo [evento:aurora].'})
    result = correlate_preview_events(rows, tenant_id='synthetic', source_account='account')
    assert result['events'][0]['current']['state'] == 'CONTESTED'
    assert result['events'][0]['relations'][-1]['type'] == 'CONTESTS'


def natural_event_messages():
    rows = synthetic_event_messages()
    texts = [
        'Proponho viagem para Serra Azul sábado.',
        'Sábado não será possível; proponho domingo às 09:00.',
        'Combinado domingo às 09:00.',
        'Estou ciente da viagem para Serra Azul domingo.',
        'Proponho passeio para Serra Azul domingo.',
        'Proponho viagem para Vale Verde domingo.',
        'Talvez confirme domingo.',
    ]
    for row, text in zip(rows, texts, strict=True):
        row['text'] = text
    rows[1]['reply_message_id'] = 'm10'
    rows[2]['reply_message_id'] = 'o12'
    rows[6]['reply_message_id'] = 'm13'
    return rows


def test_natural_event_evolution_and_queries(tmp_path):
    import json
    from attention_router.application.conversation_preview import correlate_preview_events, answer_preview_question
    scope = dict(tenant_id='synthetic', source_account='account')
    rows = natural_event_messages()
    view = correlate_preview_events(rows[::-1], **scope)
    event = next(e for e in view['events'] if e['history'][0]['message_id'] == 'm10')
    assert event['current']['event_time']['date'] == '2026-11-15'
    assert event['current']['author'] == 'Maria'
    assert event['history'][0]['superseded_by'] == 'o12'
    assert event['history'][0]['event_time']['date'] == '2026-11-14'
    assert len(view['hypotheses']) == 3  # Serra Azul candidates; Vale Verde never joins
    assert all('other' not in h['evidence_ids'] for h in view['hypotheses'])
    assert all(h['status'] == 'HYPOTHESIS' for h in view['hypotheses'])
    questions = ['O que ficou combinado sobre a viagem?', 'Quem confirmou a mudança?',
                 'Quando a data foi alterada?', 'O que Maria falou anteriormente?',
                 'João estava sabendo da mudança?', 'Qual é o estado mais recente do planejamento?',
                 'Quais mensagens sustentam essa resposta?']
    answers = [answer_preview_question(view, **scope, question=q, event_id=event['event_id'],
               person='Joao' if q.startswith('João') else 'Maria' if 'anteriormente' in q else None)
               for q in questions]
    assert answers[1]['answers'][0]['declarations'][0]['author'] == 'Maria'
    assert answers[4]['status'] == 'NO_EVIDENCE'  # João's other conversation is only a hypothesis
    review = dict(**scope, decision_ref='synthetic-link', action='LINK', message_id='j13', target_message_id='m13')
    linked = correlate_preview_events(rows, **scope, reviews=[review])
    confirmed = answer_preview_question(linked, **scope, question=questions[4], event_id=event['event_id'], person='Joao')
    assert confirmed['status'] == 'SUPPORTED'
    assert confirmed['answers'][0]['declarations'][0]['association_basis'] == 'OWNER_REVIEW'
    assert view['durable_claims'] == linked['durable_claims'] == []
    (tmp_path / 'natural-events-demo.json').write_text(json.dumps(
        {'before_review': view, 'questions': dict(zip(questions, answers, strict=True)),
         'after_review': linked, 'joao_after_review': confirmed}, ensure_ascii=False, indent=2))


def test_event_reviews_redaction_replay_and_no_false_confirmation():
    from attention_router.application.conversation_preview import correlate_preview_events, answer_preview_question
    from attention_router.application.memory import preview_event_candidate
    scope = dict(tenant_id='synthetic', source_account='account')
    for text in ['Confirmo domingo?', 'Talvez confirmo domingo.', 'Maria disse confirmo domingo.', 'Não confirmo domingo.']:
        assert preview_event_candidate(text)['action'] != 'CONFIRMS'
    rows = natural_event_messages()
    review = dict(**scope, decision_ref='exclude', action='EXCLUDE', message_id='m13')
    view = correlate_preview_events(rows, **scope, reviews=[review, review])
    assert 'm13' not in view['evidence']
    assert 'm13' in view['excluded_evidence_ids']
    assert view['events'][0]['current']['state'] == 'PROPOSED'
    assert answer_preview_question(view, **scope, question='Qual é a senha?')['status'] == 'UNSUPPORTED_QUESTION'
    with pytest.raises(ValueError, match='REPLAY_CONFLICT'):
        correlate_preview_events(rows, **scope, reviews=[review, {**review, 'action':'RESTORE'}])
    link = dict(**scope, decision_ref='link', action='LINK', message_id='j13', target_message_id='m13')
    unlink = dict(**scope, decision_ref='unlink', action='UNLINK', message_id='j13')
    assert correlate_preview_events(rows, **scope, reviews=[link, unlink])['events'] == correlate_preview_events(rows, **scope)['events']


@pytest.mark.parametrize('text,status,date', [
    ('depois de amanhã', 'INTERPRETED', '2026-11-16'),
    ('ontem', 'INTERPRETED', '2026-11-13'),
    ('amanhã ou domingo', 'AMBIGUOUS', None),
    ('domingo passado', 'UNRESOLVED', None),
])
def test_relative_time_no_silent_wrong_day(text, status, date):
    from attention_router.application.conversation_preview import preview_event_time
    value = preview_event_time(text, STAMP.isoformat(), 'UTC')
    assert value['status'] == status
    assert value.get('date') == date


def test_review_link_rejects_future_and_revoked_evidence():
    from attention_router.application.conversation_preview import correlate_preview_events
    scope = dict(tenant_id='synthetic', source_account='account')
    review = dict(**scope, decision_ref='bad-link', action='LINK', message_id='m10', target_message_id='m13')
    with pytest.raises(ValueError, match='TARGET_UNAVAILABLE'):
        correlate_preview_events(natural_event_messages(), **scope, reviews=[review])


def test_mentioned_person_is_not_speaker_or_knowing_participant():
    from attention_router.application.conversation_preview import correlate_preview_events, query_preview_events
    scope = dict(tenant_id='synthetic', source_account='account')
    rows = natural_event_messages()
    rows[4]['text'] = 'Proponho passeio para Serra Azul com Maria domingo.'
    alias = dict(**scope, label='Maria', actor_id='Maria', basis='OWNER_REVIEWED_ALIAS', evidence_ref='synthetic-alias-review')
    view = correlate_preview_events(rows, **scope, person_aliases=[alias])
    result = query_preview_events(view, **scope, person='Maria', person_role='MENTIONED')
    assert result['results'][0]['matches'][0]['author'] == 'Pedro'
    assert result['results'][0]['matches'][0]['message_id'] == 'p13'
    assert query_preview_events(view, **scope, person='Maria', action='ACKNOWLEDGES')['status'] == 'NO_EVIDENCE'
    ambiguous = correlate_preview_events(rows, **scope, person_aliases=[alias, {**alias, 'actor_id':'another-Maria'}])
    assert query_preview_events(ambiguous, **scope, person='Maria', person_role='MENTIONED')['status'] == 'NO_EVIDENCE'
    with pytest.raises(ValueError, match='SCOPE'):
        correlate_preview_events(rows, **scope, person_aliases=[{**alias, 'tenant_id':'other'}])

def test_capture_deadline_respects_configured_tenant(session, monkeypatch):
    from attention_router.application.conversation_live_capture import expire_pending_capture
    from tests.integration.test_postgres_conversation_live_capture import receipt
    from tests.test_whatsapp_artifacts import _second_tenant
    monkeypatch.setattr(settings, 'conversation_capture_enabled', True)
    monkeypatch.setattr(settings, 'conversation_capture_start_at', STAMP-timedelta(minutes=1))
    monkeypatch.setattr(settings, 'conversation_capture_tenant_id', None)
    other_tenant = _second_tenant(session)
    first = receipt('synthetic-deadline-scope-a', voice=True)
    other = receipt('synthetic-deadline-scope-b', voice=True)
    other.tenant_id = other_tenant
    session.add_all([first, other])
    session.flush()
    for item in (first, other):
        assert capture_live_receipt(session, item).status == 'ARCHIVED'
    session.commit()
    monkeypatch.setattr(settings, 'conversation_capture_tenant_id', DEFAULT_TENANT_ID)
    assert expire_pending_capture(session, now=STAMP+timedelta(minutes=16)) == 1
    rows = {r.tenant_id:r for r in messages(session)}
    assert rows[DEFAULT_TENANT_ID].metadata_json['content_state'] == 'MEDIA_CAPTURE_TIMEOUT'
    assert rows[other_tenant].metadata_json['content_state'] == 'PENDING_TRANSCRIPTION'
    assert all(r.text is None for r in rows.values())
