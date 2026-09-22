from datetime import UTC, datetime, timedelta
import base64
import hashlib

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import func, select

from attention_router.adapters.wwebjs_owner_control import (
    OwnerControlParseResult,
    OwnerControlParseStatus,
)
from attention_router.application import client_command as client_command_module
from attention_router.application.client_command import (
    ClientCommandAuthorityRejected,
    ClientCommandConflict,
    ClientCommandService,
)
from attention_router.application.client_session import ClientSessionService
from attention_router.application.owner_control import (
    AutomaticResponsesEnabledParameters,
    OwnerControlAction,
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
    ActorBindingRow,
    ClientCommandMessageRow,
    FactRow,
    OwnerAutomationControlChangeRow,
    OwnerOperationalControlRow,
    PendingIntentRow,
    TenantRow,
)
from attention_router.security.device_keys import encode_unpadded_base64url
from tests.test_owner_control import _install_owner_and_grace


NOW = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
HUMAN_ID = "hid_" + "c" * 24
DEVICE_ID = "cdev_" + "d" * 24
PERSONAL_TENANT_ID = "tnt_" + "p" * 24


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
    session.add(
        TenantRow(
            id=PERSONAL_TENANT_ID,
            slug="personal-command-test",
            name="Personal",
            status="ACTIVE",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.flush()
    session.add_all(
        [
            ClientTenantMembershipRow(
                id="ctm_" + "p" * 24,
                human_identity_id=HUMAN_ID,
                tenant_id=PERSONAL_TENANT_ID,
                role="OWNER",
                status="ACTIVE",
                created_at=NOW,
                updated_at=NOW,
            ),
            ClientTenantMembershipRow(
                id="ctm_" + "o" * 24,
                human_identity_id=HUMAN_ID,
                tenant_id=DEFAULT_TENANT_ID,
                role="OWNER",
                status="ACTIVE",
                created_at=NOW,
                updated_at=NOW,
            ),
        ]
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
        requested_tenant_id=PERSONAL_TENANT_ID,
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
    stored = session.get(ClientCommandMessageRow, paused.command_id)
    assert stored.tenant_id == PERSONAL_TENANT_ID
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


def test_ambiguous_operational_owner_scope_fails_closed(session):
    sessions, issued = issue_owner_session(session)
    session.add(
        ActorBindingRow(
            id="personal-owner-binding",
            tenant_id=PERSONAL_TENANT_ID,
            source="client-session-test",
            external_actor_id="personal-owner-external",
            actor_key="actor_personal_owner",
            display_name="Personal Owner",
            actor_category="owner",
            active_context=None,
            is_active=True,
            binding_metadata={"owner": True},
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.commit()

    with pytest.raises(ClientCommandAuthorityRejected):
        service(sessions).list_recent(
            session,
            session_token=issued.session_token,
            now=NOW + timedelta(seconds=10),
        )


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


def test_free_language_semantic_resume_maps_to_canonical_action(session, monkeypatch):
    sessions, issued = issue_owner_session(session)
    commands = service(sessions)

    monkeypatch.setattr(
        client_command_module,
        "interpret_owner_control_semantically",
        lambda text: OwnerControlParseResult(
            OwnerControlParseStatus.MATCHED,
            OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED,
            AutomaticResponsesEnabledParameters(enabled=True),
        ),
    )

    result = commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="req-free-resume",
        text="voltar à vida",
        now=NOW + timedelta(seconds=20),
    )
    session.commit()

    assert result.state == "COMPLETED"
    assert result.normalized_action == "SET_AUTOMATIC_RESPONSES_ENABLED"
    assert "ativa" in (result.response_text or "").casefold()


def test_android_client_clarification_roundtrip_learns_confirmed_idiolect(
    session,
    monkeypatch,
):
    sessions, issued = issue_owner_session(session)
    commands = service(sessions)

    def semantic(text: str) -> OwnerControlParseResult:
        if text == "retorne em 30 segundos":
            return OwnerControlParseResult(
                OwnerControlParseStatus.REJECTED,
                reason_code="CONTROL_COMMAND_NEEDS_CLARIFICATION",
            )
        return OwnerControlParseResult(OwnerControlParseStatus.NOT_CONTROL_COMMAND)

    monkeypatch.setattr(
        client_command_module,
        "interpret_owner_control_semantically",
        semantic,
    )

    ambiguous = commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="req-clarify-source",
        text="retorne em 30 segundos",
        now=NOW + timedelta(seconds=30),
    )
    session.commit()

    assert ambiguous.state == "CLARIFICATION_REQUIRED"
    assert "1)" in (ambiguous.response_text or "")
    assert "2)" in (ambiguous.response_text or "")

    pending = session.scalar(select(PendingIntentRow))
    assert pending is not None
    assert pending.state == "PENDING"
    assert pending.tenant_id == DEFAULT_TENANT_ID
    assert pending.source_tenant_id == PERSONAL_TENANT_ID
    assert pending.source_client_command_id == ambiguous.command_id
    assert pending.source_channel == "android-client-command"

    resolved = commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="req-clarify-resolution",
        text="1",
        now=NOW + timedelta(seconds=31),
    )
    session.commit()
    session.refresh(pending)

    assert pending.state == "RESOLVED"
    assert pending.resolution_client_command_id == resolved.command_id
    assert resolved.state == "COMPLETED"
    assert resolved.normalized_action == "SET_OWNER_REPLY_GRACE_SECONDS"
    assert "30 segundos" in (resolved.response_text or "")

    control = session.scalar(select(OwnerOperationalControlRow))
    assert control is not None
    assert control.integer_value == 30

    learned = session.scalar(
        select(FactRow).where(
            FactRow.fact_class == "USER_CONFIRMED_LANGUAGE",
            FactRow.predicate == "idiolect.pragmatic_mapping",
        )
    )
    assert learned is not None
    assert learned.value_json["expression"] == "retorne em 30 segundos"
    assert learned.value_json["semantic_intent_key"] == "CONFIGURE_OWNER_REPLY_GRACE"
    assert learned.value_json["parameters"] == {"seconds": 30}

    repeated = commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="req-clarify-repeat",
        text="retorne em 30 segundos",
        now=NOW + timedelta(seconds=32),
    )
    session.commit()

    assert repeated.state == "COMPLETED"
    assert repeated.normalized_action == "SET_OWNER_REPLY_GRACE_SECONDS"
    assert session.scalar(
        select(func.count())
        .select_from(PendingIntentRow)
        .where(PendingIntentRow.state == "PENDING")
    ) == 0


def test_android_client_explicit_command_supersedes_pending_clarification(
    session,
    monkeypatch,
):
    sessions, issued = issue_owner_session(session)
    commands = service(sessions)

    monkeypatch.setattr(
        client_command_module,
        "interpret_owner_control_semantically",
        lambda text: (
            OwnerControlParseResult(
                OwnerControlParseStatus.REJECTED,
                reason_code="CONTROL_COMMAND_NEEDS_CLARIFICATION",
            )
            if text == "retorne em 30 segundos"
            else OwnerControlParseResult(OwnerControlParseStatus.NOT_CONTROL_COMMAND)
        ),
    )

    commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="req-supersede-source",
        text="retorne em 30 segundos",
        now=NOW + timedelta(seconds=40),
    )
    session.commit()
    pending = session.scalar(select(PendingIntentRow))
    assert pending is not None
    assert pending.state == "PENDING"

    explicit = commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="req-supersede-pare",
        text="pare",
        now=NOW + timedelta(seconds=41),
    )
    session.commit()
    session.refresh(pending)

    assert explicit.state == "COMPLETED"
    assert pending.state == "SUPERSEDED"
    assert (pending.provenance or {})["superseding_source_client_command_id"] == (
        explicit.command_id
    )
