from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from attention_router.adapters.inbound import NormalizedInboundEvent
from attention_router.adapters.wwebjs_owner_control import (
    OwnerControlParseResult,
    OwnerControlParseStatus,
)
from attention_router.application import services
from attention_router.application.owner_control_clarification import (
    OwnerClarificationResolutionKind,
    render_owner_control_clarification,
    resolve_owner_control_clarification_reply,
)
from attention_router.application.owner_control_semantic_registry import (
    OWNER_CONTROL_SEMANTIC_REGISTRY_VERSION,
    build_registered_semantic_candidate,
)
from attention_router.application.pending_intent import build_candidate_set
from attention_router.config import settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import now_utc
from attention_router.infrastructure.models import (
    ActorBindingRow,
    OutboxMessageRow,
    OwnerOperationalControlRow,
    PendingIntentRow,
)
from tests.test_owner_control import (
    OWNER_EXTERNAL_ID,
    _install_owner_and_grace,
)


CONVERSATION = "wwebjs:owner-control-channel@c.us"


@pytest.fixture()
def owner_control_session(session):
    _install_owner_and_grace(session)
    return session


def _candidate_set(*, include_one_shot: bool = True) -> dict:
    candidates = [
        build_registered_semantic_candidate(
            intent_key="CONFIGURE_OWNER_REPLY_GRACE",
            parameters={"seconds": 30},
            confidence="high",
        )
    ]
    if include_one_shot:
        candidates.append(
            build_registered_semantic_candidate(
                intent_key="ONE_SHOT_REPLY_DELAY",
                parameters={"seconds": 30},
                confidence="high",
            )
        )
    return build_candidate_set(
        candidates,
        semantic_registry_version=OWNER_CONTROL_SEMANTIC_REGISTRY_VERSION,
    )


def _event(
    event_id: str,
    text: str,
    *,
    offset_seconds: int = 0,
    conversation: str = CONVERSATION,
) -> NormalizedInboundEvent:
    stamp = now_utc() + timedelta(seconds=offset_seconds)
    return NormalizedInboundEvent(
        source="wwebjs",
        external_event_id=event_id,
        event_type="message",
        occurred_at=stamp,
        received_at=stamp,
        actor_id=OWNER_EXTERNAL_ID,
        actor_display_name="Alex",
        actor_category="owner",
        channel="whatsapp",
        content=text,
        event_origin="OWNER_COMMAND",
        owner_authenticated=True,
        metadata={
            "from_me": True,
            "owner_self_chat": True,
            "from_me_classification": "OWNER_COMMAND",
            "final_from_me_classification": "OWNER_COMMAND",
            "source_account": "default",
            "conversation_key": conversation,
            "conversation_state": "READY",
        },
    )


def _semantic_result(text: str) -> OwnerControlParseResult:
    if text == "retorne em 30 segundos":
        return OwnerControlParseResult(
            OwnerControlParseStatus.REJECTED,
            reason_code="CONTROL_COMMAND_NEEDS_CLARIFICATION",
        )
    return OwnerControlParseResult(OwnerControlParseStatus.NOT_CONTROL_COMMAND)


def _install_semantic_mocks(monkeypatch, *, include_one_shot: bool = True) -> None:
    monkeypatch.setattr(settings, "owner_control_semantic_enabled", True)
    monkeypatch.setattr(
        services,
        "interpret_owner_control_semantically",
        _semantic_result,
    )
    monkeypatch.setattr(
        services,
        "interpret_owner_control_candidates",
        lambda text: (
            _candidate_set(include_one_shot=include_one_shot)
            if text == "retorne em 30 segundos"
            else None
        ),
    )


def _start_pending(session, monkeypatch, *, include_one_shot: bool = True):
    _install_semantic_mocks(
        monkeypatch,
        include_one_shot=include_one_shot,
    )
    result = services.receive_normalized_inbound_event(
        session,
        _event("clarification-source", "retorne em 30 segundos"),
    )
    pending = session.scalar(select(PendingIntentRow))
    assert pending is not None
    return result, pending


def _outbox_for(session, interaction_id: str) -> OutboxMessageRow:
    row = session.scalar(
        select(OutboxMessageRow).where(
            OutboxMessageRow.interaction_id == interaction_id
        )
    )
    assert row is not None
    return row


def test_two_candidate_prompt_is_bounded_and_marks_unavailable():
    prompt = render_owner_control_clarification(_candidate_set())
    assert "1) mudar a espera padrão das respostas para 30 segundos" in prompt
    assert "2) esperar 30 segundos apenas nesta resposta" in prompt
    assert "ainda indisponível" in prompt
    assert 'Responda com o número da opção' in prompt


def test_bare_yes_does_not_choose_between_two_candidates():
    resolution = resolve_owner_control_clarification_reply(
        "sim",
        _candidate_set(),
    )
    assert resolution.kind == OwnerClarificationResolutionKind.UNRESOLVED
    assert resolution.candidate_key is None


def test_second_option_selects_one_shot_candidate():
    candidate_set = _candidate_set()
    resolution = resolve_owner_control_clarification_reply(
        "a segunda",
        candidate_set,
    )
    assert resolution.kind == OwnerClarificationResolutionKind.SELECT
    selected = next(
        candidate
        for candidate in candidate_set["candidates"]
        if candidate["candidate_key"] == resolution.candidate_key
    )
    assert selected["semantic_intent_key"] == "ONE_SHOT_REPLY_DELAY"


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("sim", OwnerClarificationResolutionKind.SELECT),
        ("não", OwnerClarificationResolutionKind.CANCEL),
    ],
)
def test_bare_yes_no_only_resolves_single_candidate(reply, expected):
    resolution = resolve_owner_control_clarification_reply(
        reply,
        _candidate_set(include_one_shot=False),
    )
    assert resolution.kind == expected


def test_ambiguous_command_creates_linked_pending_intent(
    owner_control_session,
    monkeypatch,
):
    result, pending = _start_pending(owner_control_session, monkeypatch)
    outbox = _outbox_for(owner_control_session, result["id"])

    assert pending.state == "PENDING"
    assert pending.clarification_outbox_id == outbox.id
    assert pending.semantic_registry_version == OWNER_CONTROL_SEMANTIC_REGISTRY_VERSION
    assert len(pending.candidate_set["candidates"]) == 2
    assert "1)" in outbox.payload["text"]
    assert "2)" in outbox.payload["text"]
    assert owner_control_session.scalar(
        select(OwnerOperationalControlRow)
    ) is None


def test_bare_yes_keeps_two_candidate_intent_pending(
    owner_control_session,
    monkeypatch,
):
    _, pending = _start_pending(owner_control_session, monkeypatch)
    result = services.receive_normalized_inbound_event(
        owner_control_session,
        _event("clarification-bare-yes", "sim", offset_seconds=10),
    )
    owner_control_session.refresh(pending)
    outbox = _outbox_for(owner_control_session, result["id"])

    assert pending.state == "PENDING"
    assert "1)" in outbox.payload["text"]
    assert "2)" in outbox.payload["text"]
    assert owner_control_session.scalar(
        select(OwnerOperationalControlRow)
    ) is None


def test_unavailable_second_option_resolves_without_execution(
    owner_control_session,
    monkeypatch,
):
    _, pending = _start_pending(owner_control_session, monkeypatch)
    result = services.receive_normalized_inbound_event(
        owner_control_session,
        _event("clarification-second", "2", offset_seconds=10),
    )
    owner_control_session.refresh(pending)
    outbox = _outbox_for(owner_control_session, result["id"])

    assert pending.state == "RESOLVED"
    selected = next(
        candidate
        for candidate in pending.candidate_set["candidates"]
        if candidate["candidate_key"] == pending.selected_candidate_key
    )
    assert selected["semantic_intent_key"] == "ONE_SHOT_REPLY_DELAY"
    assert "ainda não está disponível" in outbox.payload["text"]
    assert owner_control_session.scalar(
        select(OwnerOperationalControlRow)
    ) is None


def test_available_first_option_reenters_existing_executor(
    owner_control_session,
    monkeypatch,
):
    _, pending = _start_pending(owner_control_session, monkeypatch)
    result = services.receive_normalized_inbound_event(
        owner_control_session,
        _event("clarification-first", "1", offset_seconds=10),
    )
    owner_control_session.refresh(pending)
    outbox = _outbox_for(owner_control_session, result["id"])
    control = owner_control_session.scalar(select(OwnerOperationalControlRow))

    assert pending.state == "RESOLVED"
    selected = next(
        candidate
        for candidate in pending.candidate_set["candidates"]
        if candidate["candidate_key"] == pending.selected_candidate_key
    )
    assert selected["semantic_intent_key"] == "CONFIGURE_OWNER_REPLY_GRACE"
    assert control is not None
    assert control.integer_value == 30
    assert outbox.payload["text"] == "Espera alterada para 30 segundos."


def test_resolution_revalidates_current_authority_before_execution(
    owner_control_session,
    monkeypatch,
):
    _, pending = _start_pending(owner_control_session, monkeypatch)
    stamp = now_utc()
    owner_control_session.add(
        ActorBindingRow(
            id="second-owner-v1c",
            tenant_id=DEFAULT_TENANT_ID,
            source="test",
            external_actor_id="second-owner-v1c",
            actor_key="second-owner-v1c",
            display_name="Second Owner",
            actor_category="owner",
            active_context=None,
            is_active=True,
            binding_metadata={"owner": True},
            created_at=stamp,
            updated_at=stamp,
        )
    )
    owner_control_session.flush()

    result = services.receive_normalized_inbound_event(
        owner_control_session,
        _event("clarification-authority-changed", "1", offset_seconds=10),
    )
    owner_control_session.refresh(pending)
    outbox = _outbox_for(owner_control_session, result["id"])

    assert pending.state == "RESOLVED"
    assert owner_control_session.scalar(
        select(OwnerOperationalControlRow)
    ) is None
    assert outbox.payload["text"] == "Não consegui aplicar o comando."


def test_cancel_closes_pending_without_execution(
    owner_control_session,
    monkeypatch,
):
    _, pending = _start_pending(owner_control_session, monkeypatch)
    result = services.receive_normalized_inbound_event(
        owner_control_session,
        _event("clarification-cancel", "nenhuma", offset_seconds=10),
    )
    owner_control_session.refresh(pending)
    outbox = _outbox_for(owner_control_session, result["id"])

    assert pending.state == "CANCELED"
    assert "Não vou executar" in outbox.payload["text"]
    assert owner_control_session.scalar(
        select(OwnerOperationalControlRow)
    ) is None


def test_different_conversation_cannot_resolve_pending(
    owner_control_session,
    monkeypatch,
):
    _, pending = _start_pending(owner_control_session, monkeypatch)
    services.receive_normalized_inbound_event(
        owner_control_session,
        _event(
            "clarification-other-conversation",
            "1",
            offset_seconds=10,
            conversation="wwebjs:other-owner-thread@c.us",
        ),
    )
    owner_control_session.refresh(pending)

    assert pending.state == "PENDING"
    assert owner_control_session.scalar(
        select(OwnerOperationalControlRow)
    ) is None


def test_expired_pending_intent_is_not_resolved(
    owner_control_session,
    monkeypatch,
):
    _, pending = _start_pending(owner_control_session, monkeypatch)
    stamp = now_utc()
    pending.created_at = stamp - timedelta(seconds=2)
    pending.expires_at = stamp - timedelta(seconds=1)
    owner_control_session.flush()

    services.receive_normalized_inbound_event(
        owner_control_session,
        _event("clarification-after-expiry", "1", offset_seconds=10),
    )
    owner_control_session.refresh(pending)

    assert pending.state == "EXPIRED"
    assert owner_control_session.scalar(
        select(OwnerOperationalControlRow)
    ) is None


def test_new_explicit_command_supersedes_pending_and_executes(
    owner_control_session,
    monkeypatch,
):
    _, pending = _start_pending(owner_control_session, monkeypatch)
    result = services.receive_normalized_inbound_event(
        owner_control_session,
        _event("clarification-new-command", "espera 45", offset_seconds=10),
    )
    owner_control_session.refresh(pending)
    control = owner_control_session.scalar(select(OwnerOperationalControlRow))
    outbox = _outbox_for(owner_control_session, result["id"])

    assert pending.state == "SUPERSEDED"
    assert control is not None
    assert control.integer_value == 45
    assert outbox.payload["text"] == "Espera alterada para 45 segundos."


def test_single_candidate_yes_executes_after_resolution(
    owner_control_session,
    monkeypatch,
):
    _, pending = _start_pending(
        owner_control_session,
        monkeypatch,
        include_one_shot=False,
    )
    services.receive_normalized_inbound_event(
        owner_control_session,
        _event("clarification-single-yes", "sim", offset_seconds=10),
    )
    owner_control_session.refresh(pending)
    control = owner_control_session.scalar(select(OwnerOperationalControlRow))

    assert pending.state == "RESOLVED"
    assert control is not None
    assert control.integer_value == 30


def test_missing_conversation_key_falls_back_to_terminal_error(
    owner_control_session,
    monkeypatch,
):
    _install_semantic_mocks(monkeypatch)
    event = _event("clarification-no-conversation", "retorne em 30 segundos")
    event.metadata.pop("conversation_key")
    result = services.receive_normalized_inbound_event(
        owner_control_session,
        event,
    )
    outbox = _outbox_for(owner_control_session, result["id"])

    assert owner_control_session.scalar(select(PendingIntentRow)) is None
    assert (
        outbox.payload["text"]
        == "Não consegui ter certeza do comando. Pode reformular de forma mais direta?"
    )
    assert owner_control_session.scalar(
        select(OwnerOperationalControlRow)
    ) is None
