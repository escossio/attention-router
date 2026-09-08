from datetime import timedelta
import hashlib
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

from sqlalchemy import select

from attention_router.adapters.internal_ingress import InternalIngressAdapter
from attention_router.adapters.tts import TTSClientError
from attention_router.adapters.wwebjs_outbound import WwebjsOutboundConfigError
from attention_router.application import autonomy, execution, services
from attention_router.application.owner_reply_grace import process_due_grace_windows
from attention_router.application.voice_media import (
    MediaReadyNotification,
    cleanup_expired_media,
    record_media_notification,
)
from attention_router.application.voice_transcription import process_voice_transcriptions
from attention_router.application.voice_tts import process_tts_derivations, validate_voice_outbox
from attention_router.config import settings
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.media_store import MediaStore, MediaStoreError
from attention_router.infrastructure.models import (
    AgentDecisionRow,
    AgentExecutionIntentRow,
    ConversationResponseGraceWindowRow,
    MediaArtifactRow,
    OutboxMessageRow,
    OwnerAutomationControlRow,
    QueueRow,
    TTSDerivationRow,
    VoiceTranscriptionRow,
)
from attention_router.infrastructure.worker import process_agent_decisions
from tests.test_direct_conversation import PEER, direct_event, setup_direct


def voice_event(*, has_media=True, external_event_id=None, content="caption ignored"):
    return InternalIngressAdapter().normalize({
        "source": "wwebjs", "external_event_id": external_event_id or new_id(),
        "event_type": "message", "external_actor_id": PEER, "channel": "whatsapp",
        "message_type": "ptt", "content": content, "has_media": has_media,
        "event_origin": "EXTERNAL_INBOUND", "owner_authenticated": False,
        "metadata": {"from_me": False, "source_account": "default",
                     "conversation_state": "READY", "conversation_key": "wwebjs:" + PEER,
                     "peer_identifiers": [PEER]},
    })


def release_grace(session):
    window = session.scalar(select(ConversationResponseGraceWindowRow))
    assert process_due_grace_windows(
        session, "voice-test", timestamp=window.due_at + timedelta(seconds=1)
    ) == 1
    return window


def attach_media(session, monkeypatch, tmp_path, received, body=b"OggS voice fixture"):
    monkeypatch.setattr(settings, "whatsapp_media_root", str(tmp_path))
    reference, digest, size, _ = MediaStore(tmp_path, 5 * 1024 * 1024).put_bytes(
        body, mime_type="audio/ogg"
    )
    artifact = record_media_notification(session, MediaReadyNotification(
        source="wwebjs", external_event_id=received["external_event_id"],
        media_ref=reference, content_sha256=digest, mime_type="audio/ogg",
        size_bytes=size, media_kind="ptt", capture_status="READY",
    ))
    return artifact


def prepare_tts_pending(session, monkeypatch, tmp_path):
    setup_direct(session, monkeypatch)
    event = voice_event()
    received = services.receive_normalized_inbound_event(session, event)
    attach_media(session, monkeypatch, tmp_path, {
        **received, "external_event_id": event.external_event_id,
    })
    process_voice_transcriptions(session, "stt", client=SimpleNamespace(transcribe=lambda *_: {
        "transcript": "Responda em voz.", "provider": "openai",
        "model": "gpt-transcribe", "request_id": "opaque-stt",
    }))
    release_grace(session)
    process_agent_decisions(session, "worker")
    autonomy.evaluate_and_route(session, session.scalar(select(AgentDecisionRow)))
    execution.enqueue_ready_intents(session, transport_ready=True)
    return received


_VALID_MP3 = subprocess.run(
    [
        shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=0.25", "-ac", "1",
        "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1",
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
).stdout


class FakeTTS:
    def __init__(self):
        self.calls = 0

    def synthesize(self, _text, output_path: Path, *, language=None):
        self.calls += 1
        output_path.write_bytes(_VALID_MP3)
        return SimpleNamespace(provider="xai", request_id="opaque-tts", output_mime="audio/mpeg")


class FakeLocalTransport:
    def __init__(self, exc=None):
        self.exc = exc
        self.calls = []

    def dispatch_outbox(self, outbox):
        self.calls.append(outbox.id)
        if self.exc:
            raise self.exc
        return SimpleNamespace(status="sent", response={"message_reference": "opaque"})


def test_ptt_without_media_fails_closed_and_never_calls_agent(session, monkeypatch):
    captured = setup_direct(session, monkeypatch)
    received = services.receive_normalized_inbound_event(session, voice_event(has_media=False))
    assert received["inbound_text"] == ""
    release_grace(session)
    assert process_agent_decisions(session, "worker") == 0
    queue = session.get(QueueRow, f"decision:{received['inbound_event_id']}")
    assert queue.status == "CANCELED"
    assert captured == []
    assert session.scalar(select(AgentDecisionRow)) is None
    assert session.scalar(select(OutboxMessageRow)) is None


def test_grace_before_stt_waits_then_ready_requeues_same_event(
    session, monkeypatch, tmp_path
):
    captured = setup_direct(session, monkeypatch)
    event = voice_event()
    received = services.receive_normalized_inbound_event(session, event)
    attach_media(session, monkeypatch, tmp_path, {
        **received, "external_event_id": event.external_event_id,
    })
    release_grace(session)
    process_agent_decisions(session, "worker")
    queue = session.get(QueueRow, f"decision:{received['inbound_event_id']}")
    assert queue.status == "WAITING_TRANSCRIPTION"
    assert captured == []

    fake_stt = SimpleNamespace(transcribe=lambda _path, _mime: {
        "transcript": "Texto confiável do áudio.", "provider": "openai",
        "model": "gpt-transcribe", "request_id": "opaque",
    })
    assert process_voice_transcriptions(session, "stt-worker", client=fake_stt) == 1
    assert queue.status == "PENDING"
    assert process_agent_decisions(session, "worker") == 1
    assert captured[0].current_message == "Texto confiável do áudio."
    assert "[ptt]" not in str(captured[0].prompt_payload())
    transcription = session.scalar(select(VoiceTranscriptionRow))
    assert transcription.status == "READY"
    assert transcription.inbound_event_id == received["inbound_event_id"]


def test_voice_decision_none_during_readiness_race_is_not_marked_done(
    session, monkeypatch
):
    setup_direct(session, monkeypatch)
    received = services.receive_normalized_inbound_event(session, voice_event())
    release_grace(session)
    readiness = iter([
        ("READY", "VOICE_TRANSCRIPTION_READY"),
        ("WAITING", "VOICE_TRANSCRIPTION_PENDING"),
    ])
    monkeypatch.setattr(
        "attention_router.infrastructure.worker.voice_decision_readiness",
        lambda *_: next(readiness),
    )
    monkeypatch.setattr(
        "attention_router.infrastructure.worker.process_agent_decision",
        lambda *_: None,
    )

    assert process_agent_decisions(session, "worker") == 0
    queue = session.get(QueueRow, f"decision:{received['inbound_event_id']}")
    assert queue.status == "WAITING_TRANSCRIPTION"
    assert queue.processed_at is None


def test_stt_ready_before_grace_does_not_release_decision_early(
    session, monkeypatch, tmp_path
):
    captured = setup_direct(session, monkeypatch)
    event = voice_event()
    received = services.receive_normalized_inbound_event(session, event)
    attach_media(session, monkeypatch, tmp_path, {
        **received, "external_event_id": event.external_event_id,
    })
    fake_stt = SimpleNamespace(transcribe=lambda *_: {
        "transcript": "Pronto antes da Grace.", "provider": "openai",
        "model": "gpt-transcribe", "request_id": "opaque",
    })
    process_voice_transcriptions(session, "stt-worker", client=fake_stt)
    assert captured == []
    assert session.get(QueueRow, f"decision:{received['inbound_event_id']}") is None
    release_grace(session)
    process_agent_decisions(session, "worker")
    assert captured[0].current_message == "Pronto antes da Grace."


def test_disabled_stt_worker_keeps_transcription_pending_without_provider_call(
    session, monkeypatch, tmp_path
):
    setup_direct(session, monkeypatch)
    event = voice_event()
    received = services.receive_normalized_inbound_event(session, event)
    attach_media(session, monkeypatch, tmp_path, {
        **received, "external_event_id": event.external_event_id,
    })
    monkeypatch.setattr(settings, "stt_enabled", False)

    assert process_voice_transcriptions(session, "stt-worker") == 0
    transcription = session.scalar(select(VoiceTranscriptionRow))
    assert transcription.status == "PENDING"
    assert transcription.attempt_count == 0


def test_stale_stt_processing_is_reclaimed_and_reuses_same_inbound_event(
    session, monkeypatch, tmp_path
):
    setup_direct(session, monkeypatch)
    event = voice_event()
    received = services.receive_normalized_inbound_event(session, event)
    attach_media(session, monkeypatch, tmp_path, {
        **received, "external_event_id": event.external_event_id,
    })
    transcription = session.scalar(select(VoiceTranscriptionRow))
    transcription.status = "PROCESSING"
    transcription.attempt_count = 1
    transcription.claimed_at = now_utc() - timedelta(
        seconds=settings.stt_processing_lease_seconds + 1
    )
    transcription.claimed_by = "crashed-worker"
    session.commit()
    calls = []
    client = SimpleNamespace(transcribe=lambda *_: calls.append("called") or {
        "transcript": "Recuperado.", "provider": "openai",
        "model": "gpt-transcribe", "request_id": "opaque",
    })

    assert process_voice_transcriptions(session, "replacement", client=client) == 1
    session.refresh(transcription)
    assert transcription.status == "READY"
    assert transcription.inbound_event_id == received["inbound_event_id"]
    assert transcription.attempt_count == 2
    assert transcription.error_code is None
    assert calls == ["called"]


def test_fresh_stt_processing_claim_is_not_stolen(session, monkeypatch, tmp_path):
    setup_direct(session, monkeypatch)
    event = voice_event()
    received = services.receive_normalized_inbound_event(session, event)
    attach_media(session, monkeypatch, tmp_path, {
        **received, "external_event_id": event.external_event_id,
    })
    transcription = session.scalar(select(VoiceTranscriptionRow))
    transcription.status = "PROCESSING"
    transcription.claimed_at = now_utc()
    transcription.claimed_by = "live-worker"
    session.commit()
    calls = []

    assert process_voice_transcriptions(
        session,
        "second-worker",
        client=SimpleNamespace(transcribe=lambda *_: calls.append("called")),
    ) == 0
    session.refresh(transcription)
    assert transcription.status == "PROCESSING"
    assert transcription.claimed_by == "live-worker"
    assert calls == []


def test_owner_reply_during_stt_prevents_ready_reviving_queue(
    session, monkeypatch, tmp_path
):
    captured = setup_direct(session, monkeypatch)
    event = voice_event()
    received = services.receive_normalized_inbound_event(session, event)
    attach_media(session, monkeypatch, tmp_path, {
        **received, "external_event_id": event.external_event_id,
    })
    window = release_grace(session)
    process_agent_decisions(session, "worker")
    observation = direct_event(
        "5500000000027@c.us", external_event_id=new_id(), content="",
        occurred_at=window.last_inbound_at + timedelta(seconds=1),
        received_at=window.last_inbound_at + timedelta(seconds=1),
        event_origin="OWNER_MANUAL_OUTBOUND_OBSERVED",
        metadata={"from_me": True, "source_account": "default",
                  "conversation_state": "READY", "conversation_key": "wwebjs:" + PEER,
                  "peer_identifiers": [PEER],
                  "from_me_classification": "OWNER_MANUAL_OUTBOUND_OBSERVED",
                  "message_type": "chat"},
    )
    services.receive_normalized_inbound_event(session, observation)
    fake_stt = SimpleNamespace(transcribe=lambda *_: {
        "transcript": "Não ressuscitar.", "provider": "openai",
        "model": "gpt-transcribe", "request_id": "opaque",
    })
    process_voice_transcriptions(session, "stt-worker", client=fake_stt)
    queue = session.get(QueueRow, f"decision:{received['inbound_event_id']}")
    assert queue.status == "CANCELED"
    assert captured == []


def test_authorized_voice_decision_creates_tts_then_voice_outbox(
    session, monkeypatch, tmp_path
):
    setup_direct(session, monkeypatch)
    event = voice_event()
    received = services.receive_normalized_inbound_event(session, event)
    attach_media(session, monkeypatch, tmp_path, {
        **received, "external_event_id": event.external_event_id,
    })
    process_voice_transcriptions(session, "stt", client=SimpleNamespace(transcribe=lambda *_: {
        "transcript": "Responda em voz.", "provider": "openai",
        "model": "gpt-transcribe", "request_id": "opaque-stt",
    }))
    release_grace(session)
    process_agent_decisions(session, "worker")
    decision = session.scalar(select(AgentDecisionRow))
    autonomy.evaluate_and_route(session, decision)
    intent = session.scalar(select(AgentExecutionIntentRow))
    assert execution.enqueue_ready_intents(session, transport_ready=True) == 1
    assert session.scalar(select(OutboxMessageRow)) is None
    assert session.scalar(select(TTSDerivationRow)).status == "PENDING"

    assert process_tts_derivations(session, "tts", client=FakeTTS()) == 1
    outbox = session.scalar(select(OutboxMessageRow))
    artifact = session.scalar(select(MediaArtifactRow).where(MediaArtifactRow.direction == "OUTBOUND"))
    assert outbox.action_type == "agent_execution_voice"
    assert "text" not in outbox.payload and "transcript" not in outbox.payload
    assert artifact.content_sha256 == outbox.payload["content_sha256"]
    validate_voice_outbox(session, outbox, intent)
    artifact_path = MediaStore(tmp_path, settings.whatsapp_media_max_bytes).path_for_ref(
        outbox.payload["media_ref"]
    )
    artifact_path.write_bytes(b"tampered")
    try:
        validate_voice_outbox(session, outbox, intent)
    except MediaStoreError as exc:
        assert str(exc) in {"MEDIA_SIZE_MISMATCH", "MEDIA_HASH_MISMATCH"}
    else:
        raise AssertionError("tampered artifact passed validation")


def test_disabled_default_tts_keeps_pending_without_provider_or_outbox(
    session, monkeypatch, tmp_path
):
    prepare_tts_pending(session, monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "tts_enabled", False)
    provider_calls = []
    monkeypatch.setattr(
        "attention_router.application.voice_tts.SwitcherTTSClient",
        lambda: provider_calls.append("constructed"),
    )

    assert process_tts_derivations(session, "tts") == 0
    assert session.scalar(select(TTSDerivationRow)).status == "PENDING"
    assert session.scalar(select(AgentExecutionIntentRow)).status == "TTS_PENDING"
    assert session.scalar(select(OutboxMessageRow)) is None
    assert provider_calls == []


def test_enabled_default_tts_uses_provider_and_completes_flow(
    session, monkeypatch, tmp_path
):
    prepare_tts_pending(session, monkeypatch, tmp_path)
    client = FakeTTS()
    monkeypatch.setattr(settings, "tts_enabled", True)
    monkeypatch.setattr(settings, "tts_internal_token", "configured-for-test")
    monkeypatch.setattr(
        "attention_router.application.voice_tts.SwitcherTTSClient",
        lambda: client,
    )

    assert process_tts_derivations(session, "tts") == 1
    assert session.scalar(select(TTSDerivationRow)).status == "READY"
    assert session.scalar(select(OutboxMessageRow)).status == "PENDING"
    artifact = session.scalar(select(MediaArtifactRow).where(MediaArtifactRow.direction == "OUTBOUND"))
    outbox = session.scalar(select(OutboxMessageRow))
    assert artifact.mime_type == "audio/ogg"
    assert outbox.payload["mime_type"] == "audio/ogg"
    assert client.calls == 1


def test_stale_tts_processing_is_reclaimed_without_duplicate_logical_outbox(
    session, monkeypatch, tmp_path
):
    prepare_tts_pending(session, monkeypatch, tmp_path)
    derivation = session.scalar(select(TTSDerivationRow))
    derivation.status = "PROCESSING"
    derivation.attempt_count = 1
    derivation.claimed_at = now_utc() - timedelta(
        seconds=settings.tts_processing_lease_seconds + 1
    )
    derivation.claimed_by = "crashed-worker"
    session.commit()
    client = FakeTTS()

    assert process_tts_derivations(session, "replacement", client=client) == 1
    session.refresh(derivation)
    assert derivation.status == "READY"
    assert derivation.attempt_count == 2
    assert derivation.error_code is None
    assert client.calls == 1
    outboxes = list(session.scalars(select(OutboxMessageRow)).all())
    assert len(outboxes) == 1
    assert outboxes[0].idempotency_key == (
        f"execution:{derivation.execution_intent_id}"
    )


def test_fresh_tts_processing_claim_is_not_stolen(session, monkeypatch, tmp_path):
    prepare_tts_pending(session, monkeypatch, tmp_path)
    derivation = session.scalar(select(TTSDerivationRow))
    derivation.status = "PROCESSING"
    derivation.claimed_at = now_utc()
    derivation.claimed_by = "live-worker"
    session.commit()
    client = FakeTTS()

    assert process_tts_derivations(session, "second-worker", client=client) == 0
    session.refresh(derivation)
    assert derivation.status == "PROCESSING"
    assert derivation.claimed_by == "live-worker"
    assert client.calls == 0


def test_stale_voice_outbox_becomes_ambiguous_without_second_send(
    session, monkeypatch, tmp_path
):
    prepare_tts_pending(session, monkeypatch, tmp_path)
    process_tts_derivations(session, "tts", client=FakeTTS())
    outbox = session.scalar(select(OutboxMessageRow))
    artifact = session.scalar(select(MediaArtifactRow).where(
        MediaArtifactRow.purpose == "TTS_OUTPUT"
    ))
    outbox.status = "PROCESSING"
    outbox.claimed_at = now_utc() - timedelta(
        seconds=settings.worker_timer_lease_seconds + 1
    )
    outbox.claimed_by = "crashed-worker"
    outbox.attempt_count = 1
    session.commit()
    adapter = FakeLocalTransport()
    monkeypatch.setattr(services, "local_transport_outbound", adapter)

    assert services.process_outbox(session, "replacement") == 0
    session.refresh(outbox)
    session.refresh(artifact)
    assert outbox.status == "AMBIGUOUS"
    assert artifact.status == "AMBIGUOUS"
    assert artifact.terminal_at is not None
    assert artifact.expires_at - artifact.terminal_at == timedelta(
        hours=settings.media_ambiguous_retention_hours
    )
    assert services.process_outbox(session, "later") == 0
    assert adapter.calls == []


def test_legacy_retry_voice_outbox_is_quarantined_without_send(
    session, monkeypatch, tmp_path
):
    prepare_tts_pending(session, monkeypatch, tmp_path)
    process_tts_derivations(session, "tts", client=FakeTTS())
    outbox = session.scalar(select(OutboxMessageRow))
    outbox.status = "RETRY"
    outbox.attempt_count = 1
    session.commit()
    adapter = FakeLocalTransport()
    monkeypatch.setattr(services, "local_transport_outbound", adapter)

    assert services.process_outbox(session, "replacement") == 0
    assert outbox.status == "AMBIGUOUS"
    assert outbox.last_error == "LEGACY_EXTERNAL_RETRY_QUARANTINED"
    assert adapter.calls == []


def test_voice_contract_block_marks_normal_retention_without_network_attempt(
    session, monkeypatch, tmp_path
):
    prepare_tts_pending(session, monkeypatch, tmp_path)
    process_tts_derivations(session, "tts", client=FakeTTS())
    outbox = session.scalar(select(OutboxMessageRow))
    artifact = session.scalar(select(MediaArtifactRow).where(
        MediaArtifactRow.purpose == "TTS_OUTPUT"
    ))
    outbox.payload = {**outbox.payload, "content_sha256": "0" * 64}
    adapter = FakeLocalTransport()
    monkeypatch.setattr(services, "local_transport_outbound", adapter)

    assert services.process_outbox(session, "worker") == 1
    session.refresh(artifact)
    assert outbox.status == "BLOCKED"
    assert artifact.status == "READY"
    assert artifact.expires_at - artifact.terminal_at == timedelta(
        hours=settings.media_retention_hours
    )
    assert adapter.calls == []


def test_voice_human_cancel_marks_normal_retention_without_network_attempt(
    session, monkeypatch, tmp_path
):
    prepare_tts_pending(session, monkeypatch, tmp_path)
    process_tts_derivations(session, "tts", client=FakeTTS())
    outbox = session.scalar(select(OutboxMessageRow))
    artifact = session.scalar(select(MediaArtifactRow).where(
        MediaArtifactRow.purpose == "TTS_OUTPUT"
    ))
    adapter = FakeLocalTransport()
    monkeypatch.setattr(services, "local_transport_outbound", adapter)
    monkeypatch.setattr(services, "grace_allows_interaction", lambda *_args, **_kwargs: False)

    assert services.process_outbox(session, "worker") == 0
    session.refresh(artifact)
    assert outbox.status == "CANCELED"
    assert outbox.last_error == "CANCELED_BY_HUMAN_REPLY"
    assert artifact.status == "READY"
    assert artifact.expires_at - artifact.terminal_at == timedelta(
        hours=settings.media_retention_hours
    )
    assert adapter.calls == []


def test_voice_configuration_failure_marks_normal_retention_without_network_attempt(
    session, monkeypatch, tmp_path
):
    prepare_tts_pending(session, monkeypatch, tmp_path)
    process_tts_derivations(session, "tts", client=FakeTTS())
    outbox = session.scalar(select(OutboxMessageRow))
    artifact = session.scalar(select(MediaArtifactRow).where(
        MediaArtifactRow.purpose == "TTS_OUTPUT"
    ))
    adapter = FakeLocalTransport(WwebjsOutboundConfigError("missing test config"))
    monkeypatch.setattr(services, "local_transport_outbound", adapter)

    assert services.process_outbox(session, "worker") == 1
    session.refresh(artifact)
    assert outbox.status == "FAILED"
    assert outbox.last_error == "TRANSPORT_CONFIGURATION_DENIED"
    assert artifact.status == "READY"
    assert artifact.expires_at - artifact.terminal_at == timedelta(
        hours=settings.media_retention_hours
    )
    assert len(adapter.calls) == 1


def test_paused_voice_outbox_stays_pending_and_artifact_nonterminal(
    session, monkeypatch, tmp_path
):
    prepare_tts_pending(session, monkeypatch, tmp_path)
    process_tts_derivations(session, "tts", client=FakeTTS())
    outbox = session.scalar(select(OutboxMessageRow))
    artifact = session.scalar(select(MediaArtifactRow).where(
        MediaArtifactRow.purpose == "TTS_OUTPUT"
    ))
    stamp = now_utc()
    session.add(OwnerAutomationControlRow(
        id=new_id(), tenant_id=artifact.tenant_id,
        represented_owner_actor_key="owner_direct_test",
        automatic_responses_enabled=False, revision=1,
        updated_by_actor_key="owner_direct_test", authorization_source="TEST",
        source_channel="test", source_event_id=new_id(), provenance={},
        created_at=stamp, updated_at=stamp,
    ))
    session.commit()
    adapter = FakeLocalTransport()
    monkeypatch.setattr(services, "local_transport_outbound", adapter)

    assert services.process_outbox(session, "worker") == 0
    session.refresh(artifact)
    assert outbox.status == "PENDING"
    assert outbox.last_error == "OWNER_AUTOMATION_PAUSED"
    assert artifact.terminal_at is None
    assert artifact.expires_at is None
    assert adapter.calls == []


def test_voice_reconciliation_required_uses_ambiguous_retention(
    session, monkeypatch, tmp_path
):
    prepare_tts_pending(session, monkeypatch, tmp_path)
    process_tts_derivations(session, "tts", client=FakeTTS())
    outbox = session.scalar(select(OutboxMessageRow))
    artifact = session.scalar(select(MediaArtifactRow).where(
        MediaArtifactRow.purpose == "TTS_OUTPUT"
    ))

    services._mark_external_reconciliation_required(
        session, outbox, "PROVIDER_CONFIRMED_LOCAL_FINALIZATION_FAILED"
    )
    session.flush()
    assert outbox.status == "RECONCILIATION_REQUIRED"
    assert artifact.status == "AMBIGUOUS"
    assert artifact.expires_at - artifact.terminal_at == timedelta(
        hours=settings.media_ambiguous_retention_hours
    )


def test_tts_auto_send_does_not_turn_text_input_into_voice(
    session, monkeypatch
):
    setup_direct(session, monkeypatch)
    monkeypatch.setattr(settings, "tts_auto_send", True)
    services.receive_normalized_inbound_event(session, direct_event())
    release_grace(session)
    process_agent_decisions(session, "worker")
    decision = session.scalar(select(AgentDecisionRow))
    autonomy.evaluate_and_route(session, decision)

    assert execution.enqueue_ready_intents(session, transport_ready=True) == 1
    outbox = session.scalar(select(OutboxMessageRow))
    assert outbox.action_type == "agent_execution_text"
    assert session.scalar(select(TTSDerivationRow)) is None


def test_shared_digest_keeps_bytes_until_final_live_reference_expires(
    session, monkeypatch, tmp_path
):
    setup_direct(session, monkeypatch)
    event = voice_event()
    received = services.receive_normalized_inbound_event(session, event)
    first = attach_media(session, monkeypatch, tmp_path, {
        **received, "external_event_id": event.external_event_id,
    })
    second = MediaArtifactRow(
        id=new_id(), tenant_id=first.tenant_id, direction="OUTBOUND",
        purpose="TTS_OUTPUT", inbound_event_id=None, execution_intent_id=None,
        content_sha256=first.content_sha256, media_kind="ptt",
        mime_type=first.mime_type, size_bytes=first.size_bytes, status="READY",
        created_at=now_utc(), terminal_at=None, expires_at=None,
        provenance={"fixture": "shared-digest"},
    )
    session.add(second)
    first.terminal_at = now_utc() - timedelta(hours=25)
    first.expires_at = now_utc() - timedelta(seconds=1)
    session.commit()
    path = MediaStore(tmp_path, settings.whatsapp_media_max_bytes).path_for_digest(
        first.content_sha256
    )

    assert cleanup_expired_media(session) == 1
    assert first.status == "DELETED"
    assert path.exists()
    MediaStore(tmp_path, settings.whatsapp_media_max_bytes).validate(
        f"sha256:{second.content_sha256}", expected_sha256=second.content_sha256,
        expected_size=second.size_bytes, mime_type=second.mime_type,
    )

    second.terminal_at = now_utc() - timedelta(hours=25)
    second.expires_at = now_utc() - timedelta(seconds=1)
    session.commit()
    assert cleanup_expired_media(session) == 1
    assert second.status == "DELETED"
    assert not path.exists()


def test_numeric_response_keeps_pt_br_at_tts_boundary(session, monkeypatch, tmp_path):
    prepare_tts_pending(session, monkeypatch, tmp_path)
    intent = session.scalar(select(AgentExecutionIntentRow))
    derivation = session.scalar(select(TTSDerivationRow))
    intent.effective_response_snapshot = "20"
    derivation.source_text_hash = hashlib.sha256(b"20").hexdigest()
    session.commit()

    class LocaleTTS(FakeTTS):
        language = None

        def synthesize(self, text, output_path: Path, *, language=None):
            self.language = language
            return super().synthesize(text, output_path, language=language)

    client = LocaleTTS()
    assert process_tts_derivations(session, "tts", client=client) == 1
    assert client.language == "pt-BR"


def test_tts_terminal_failure_has_no_text_fallback(session, monkeypatch, tmp_path):
    setup_direct(session, monkeypatch)
    event = voice_event()
    received = services.receive_normalized_inbound_event(session, event)
    attach_media(session, monkeypatch, tmp_path, {
        **received, "external_event_id": event.external_event_id,
    })
    process_voice_transcriptions(session, "stt", client=SimpleNamespace(transcribe=lambda *_: {
        "transcript": "Responda.", "provider": "openai", "model": "gpt-transcribe",
        "request_id": "opaque",
    }))
    release_grace(session)
    process_agent_decisions(session, "worker")
    autonomy.evaluate_and_route(session, session.scalar(select(AgentDecisionRow)))
    execution.enqueue_ready_intents(session, transport_ready=True)

    class FailingTTS:
        def synthesize(self, *_, **__):
            raise TTSClientError("tts_timeout")

    process_tts_derivations(session, "tts", client=FailingTTS())
    process_tts_derivations(session, "tts", client=FailingTTS())
    derivation = session.scalar(select(TTSDerivationRow))
    assert derivation.status == "FAILED" and derivation.error_code == "TTS_DERIVATION_FAILED"
    assert session.scalar(select(OutboxMessageRow)) is None
