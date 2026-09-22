from datetime import UTC, datetime, timedelta
import base64
import hashlib

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import func, select

from attention_router.application.client_command import (
    ClientCommandConflict,
    ClientCommandService,
    ClientCommandVoiceInvalid,
)
from attention_router.application.client_session import ClientSessionService
from attention_router.application.speech_transcription import (
    SpeechTranscriptionResult,
)
from attention_router.application.owner_automation_control import (
    OWNER_AUTOMATION_PAUSED,
    automation_denial_reason,
)
from attention_router.config import Settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.client_bootstrap_models import (
    ClientDeviceRow,
    ClientTenantMembershipRow,
)
from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.models import (
    ClientCommandMessageRow,
    OwnerAutomationControlChangeRow,
    OwnerOperationalControlRow,
)
from attention_router.security.device_keys import encode_unpadded_base64url
from tests.test_owner_control import _install_owner_and_grace


NOW = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
HUMAN_ID = "hid_" + "c" * 24
DEVICE_ID = "cdev_" + "d" * 24


def command_settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        admin_auth_enabled=False,
        internal_ingress_hmac_secret="x" * 32,
        client_session_enabled=True,
        client_session_challenge_ttl_seconds=300,
        client_session_ttl_seconds=900,
        client_command_enabled=True,
        owner_control_semantic_enabled=False,
    )


def voice_settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        admin_auth_enabled=False,
        internal_ingress_hmac_secret="x" * 32,
        client_session_enabled=True,
        client_session_challenge_ttl_seconds=300,
        client_session_ttl_seconds=900,
        client_command_enabled=True,
        client_command_voice_enabled=True,
        client_command_voice_max_bytes=1024 * 1024,
        stt_enabled=True,
        stt_internal_token="synthetic-stt-token",
        owner_control_semantic_enabled=False,
    )


class FakeTranscriber:
    def __init__(self, transcript: str):
        self.transcript = transcript
        self.calls = 0

    def transcribe_bytes(self, audio, *, mime_type, max_bytes):
        self.calls += 1
        assert audio
        assert mime_type == "audio/mp4"
        assert max_bytes == 1024 * 1024
        return SpeechTranscriptionResult(
            transcript=self.transcript,
            provider="synthetic",
            model="synthetic-stt",
            request_id="opaque-synthetic-request",
        )


def keypair():
    private = ec.generate_private_key(ec.SECP256R1())
    spki = private.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, spki, encode_unpadded_base64url(spki)


def sign(private, challenge: str) -> str:
    raw = base64.urlsafe_b64decode(challenge + "=" * (-len(challenge) % 4))
    return encode_unpadded_base64url(
        private.sign(raw, ec.ECDSA(hashes.SHA256()))
    )


def issue_owner_session(session):
    _install_owner_and_grace(session)
    private, spki, public = keypair()
    session.add(HumanIdentityRow(id=HUMAN_ID, created_at=NOW))
    session.flush()
    session.add(
        ClientTenantMembershipRow(
            id="ctm_" + "m" * 24,
            human_identity_id=HUMAN_ID,
            tenant_id=DEFAULT_TENANT_ID,
            role="OWNER",
            status="ACTIVE",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.add(
        ClientDeviceRow(
            id=DEVICE_ID,
            human_identity_id=HUMAN_ID,
            public_key_fingerprint="sha256:" + hashlib.sha256(spki).hexdigest(),
            public_key_spki=spki,
            canonical_name="Synthetic Android",
            platform="ANDROID",
            roles=["CAPABILITY_NODE", "CLIENT"],
            status="ACTIVE",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.commit()

    sessions = ClientSessionService(settings=command_settings())
    challenge = sessions.start_session(
        session,
        public_key_spki_b64url=public,
        requested_tenant_id=DEFAULT_TENANT_ID,
        now=NOW + timedelta(seconds=1),
    )
    issued = sessions.complete_session(
        session,
        session_challenge_id=challenge.session_challenge_id,
        device_signature_b64url=sign(private, challenge.challenge_b64url),
        now=NOW + timedelta(seconds=2),
    )
    session.commit()
    return sessions, issued


def service(sessions):
    return ClientCommandService(
        settings=command_settings(),
        client_sessions=sessions,
    )


def test_pause_resume_and_replay_use_client_session_owner_authority(session):
    sessions, issued = issue_owner_session(session)
    commands = service(sessions)

    paused = commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="req-pause-1",
        text="pare",
        now=NOW + timedelta(seconds=3),
    )
    session.commit()

    assert paused.state == "COMPLETED"
    assert paused.normalized_action == "SET_AUTOMATIC_RESPONSES_ENABLED"
    assert "pausada" in (paused.response_text or "").casefold()
    assert automation_denial_reason(session, DEFAULT_TENANT_ID) == OWNER_AUTOMATION_PAUSED

    replay = commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="req-pause-1",
        text="pare",
        now=NOW + timedelta(seconds=4),
    )
    session.commit()
    assert replay.command_id == paused.command_id
    assert (
        session.scalar(select(func.count()).select_from(ClientCommandMessageRow))
        == 1
    )
    assert (
        session.scalar(
            select(func.count()).select_from(OwnerAutomationControlChangeRow)
        )
        == 1
    )

    with pytest.raises(ClientCommandConflict):
        commands.submit_text(
            session,
            session_token=issued.session_token,
            client_request_id="req-pause-1",
            text="retome",
            now=NOW + timedelta(seconds=5),
        )

    resumed = commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="req-resume-1",
        text="retome",
        now=NOW + timedelta(seconds=6),
    )
    session.commit()
    assert resumed.state == "COMPLETED"
    assert "retomada" in (resumed.response_text or "").casefold()
    assert automation_denial_reason(session, DEFAULT_TENANT_ID) is None


def test_wait_seconds_and_general_task_are_separate(session):
    sessions, issued = issue_owner_session(session)
    commands = service(sessions)

    wait = commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="req-wait-30",
        text="espera 30",
        now=NOW + timedelta(seconds=3),
    )
    session.commit()

    assert wait.state == "COMPLETED"
    assert wait.normalized_action == "SET_OWNER_REPLY_GRACE_SECONDS"
    assert "30 segundos" in (wait.response_text or "")
    control = session.scalar(select(OwnerOperationalControlRow))
    assert control is not None
    assert control.integer_value == 30

    task = commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="req-general-1",
        text="veja meu Gmail e me diga o que chegou",
        now=NOW + timedelta(seconds=4),
    )
    session.commit()

    assert task.state == "GENERAL_TASK_PENDING"
    assert task.normalized_action is None
    assert "fluxo geral de tarefas" in (task.response_text or "")


def test_recent_timeline_is_scoped_to_authenticated_human(session):
    sessions, issued = issue_owner_session(session)
    commands = service(sessions)
    for index, text in enumerate(("pare", "retome"), start=1):
        commands.submit_text(
            session,
            session_token=issued.session_token,
            client_request_id=f"req-{index}",
            text=text,
            now=NOW + timedelta(seconds=index + 2),
        )
        session.commit()

    recent = commands.list_recent(
        session,
        session_token=issued.session_token,
        limit=50,
        now=NOW + timedelta(seconds=10),
    )
    assert [item.input_text for item in recent] == ["pare", "retome"]


def test_voice_transcript_uses_same_pause_control_and_replay_skips_stt(session):
    sessions, issued = issue_owner_session(session)
    transcriber = FakeTranscriber("pare")
    commands = ClientCommandService(
        settings=voice_settings(),
        client_sessions=sessions,
        speech_transcriber=transcriber,
    )

    paused = commands.submit_voice(
        session,
        session_token=issued.session_token,
        client_request_id="voice-pause-1",
        audio=b"synthetic-mp4-bytes",
        mime_type="audio/mp4",
        now=NOW + timedelta(seconds=3),
    )
    session.commit()

    assert paused.modality == "VOICE"
    assert paused.input_text == "pare"
    assert paused.state == "COMPLETED"
    assert paused.normalized_action == "SET_AUTOMATIC_RESPONSES_ENABLED"
    assert automation_denial_reason(session, DEFAULT_TENANT_ID) == OWNER_AUTOMATION_PAUSED
    assert transcriber.calls == 1

    replay = commands.submit_voice(
        session,
        session_token=issued.session_token,
        client_request_id="voice-pause-1",
        audio=b"different-retry-bytes",
        mime_type="audio/mp4",
        now=NOW + timedelta(seconds=4),
    )
    session.commit()

    assert replay.command_id == paused.command_id
    assert transcriber.calls == 1
    assert (
        session.scalar(
            select(func.count()).select_from(OwnerAutomationControlChangeRow)
        )
        == 1
    )


def test_voice_invalid_mime_fails_before_stt(session):
    sessions, issued = issue_owner_session(session)
    transcriber = FakeTranscriber("pare")
    commands = ClientCommandService(
        settings=voice_settings(),
        client_sessions=sessions,
        speech_transcriber=transcriber,
    )

    with pytest.raises(ClientCommandVoiceInvalid):
        commands.submit_voice(
            session,
            session_token=issued.session_token,
            client_request_id="voice-invalid",
            audio=b"bytes",
            mime_type="audio/wav",
            now=NOW + timedelta(seconds=3),
        )
    assert transcriber.calls == 0


def test_voice_and_text_share_timeline(session):
    sessions, issued = issue_owner_session(session)
    transcriber = FakeTranscriber("retome")
    commands = ClientCommandService(
        settings=voice_settings(),
        client_sessions=sessions,
        speech_transcriber=transcriber,
    )

    commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="text-pause",
        text="pare",
        now=NOW + timedelta(seconds=3),
    )
    commands.submit_voice(
        session,
        session_token=issued.session_token,
        client_request_id="voice-resume",
        audio=b"synthetic-mp4",
        mime_type="audio/mp4",
        now=NOW + timedelta(seconds=4),
    )
    session.commit()

    timeline = commands.list_recent(
        session,
        session_token=issued.session_token,
        limit=50,
        now=NOW + timedelta(seconds=5),
    )
    assert [(item.modality, item.input_text) for item in timeline] == [
        ("TEXT", "pare"),
        ("VOICE", "retome"),
    ]
