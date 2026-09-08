from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from attention_router.adapters.inbound import NormalizedInboundEvent
from attention_router.adapters.internal_ingress import InternalInboundPayload
from attention_router.adapters.wwebjs_outbound import WwebjsOutboundError
from attention_router.adapters.wwebjs_owner_control import (
    OwnerControlParseStatus,
    parse_owner_grace_control,
)
from attention_router.application import services
from attention_router.application.decision_pipeline import _recent_agent_turns
from attention_router.application.owner_control import (
    AutomaticResponsesEnabledParameters,
    GraceEnabledParameters,
    GraceSecondsParameters,
    OwnerControlAction,
    OwnerControlAuthorityEvidence,
    OwnerControlError,
    OwnerControlSignal,
    OwnerControlSignalKind,
    build_owner_operator_authority,
    resolve_owner_reply_grace_policy_id,
)
from attention_router.application.platform.entities import create_relationship
from attention_router.core.entities import EntityReference
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    ActorBindingRow,
    AgentDecisionRow,
    AgentExecutionIntentRow,
    AuditEventRow,
    ConversationResponseGraceWindowRow,
    DecisionRow,
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
    OwnerOperationalControlChangeRow,
    OwnerOperationalControlRow,
    OwnerAutomationControlRow,
    PolicyRow,
    PolicyVersionRow,
    QueueRow,
)
from attention_router.infrastructure.repository import (
    create_inbound_event,
    ensure_policy_version,
    provision_morgan_owner_reply_grace_policy,
)


OWNER_ACTOR_ID = "actor_owner_control_channel"
OWNER_EXTERNAL_ID = "owner-control-channel@c.us"
OWNER_BINDING_ID = "owner-control-channel-binding"
PEER_ACTOR_ID = "actor_owner_control_peer"
PEER_EXTERNAL_ID = "owner-control-peer@lid"
PEER_BINDING_ID = "owner-control-peer-binding"
GRACE_POLICY_ID = "morgan_presence_autonomy_v1"
GRACE_AUDIENCE = "morgan_presence_autonomy"
GRACE_BINDING_ID = "actor_morgan_owner_reply_grace"


def _base_policy_config(identifier: str = GRACE_POLICY_ID) -> dict:
    return {
        "identifier": identifier,
        "name": f"Owner Control Grace {identifier}",
        "match_criteria": {
            "audience": GRACE_AUDIENCE,
            "binding_id": GRACE_BINDING_ID,
        },
        "priority": 2000,
        "specificity": 2000,
        "tone": "cordial",
        "initial_wait_seconds": 1,
        "allowed_disclosures": ["availability_hint"],
        "allowed_actions": ["respond"],
        "escalation_steps": ["respond"],
        "ack_timeout_seconds": 30,
        "repetition_limit": 1,
        "cancellation_conditions": ["human_reply"],
        "completion_conditions": ["safe_completion"],
        "execution_mode": "AUTO_ALLOWED",
        "execution_scope": {
            "actor_ids": [PEER_ACTOR_ID],
            "binding_ids": [PEER_BINDING_ID],
            "audiences": [GRACE_AUDIENCE],
        },
    }


def _add_policy(session: Session, config: dict, *, provision: bool = False) -> PolicyRow:
    row = PolicyRow(
        **{
            key: config[key]
            for key in PolicyRow.__table__.columns.keys()
            if key in config
        },
        tenant_id=DEFAULT_TENANT_ID,
        is_active=True,
    )
    session.add(row)
    session.flush()
    ensure_policy_version(session, row, config, "owner-control-test")
    if provision:
        provision_morgan_owner_reply_grace_policy(
            session,
            origin="owner-control-test",
        )
    session.flush()
    return row


def _install_owner_and_grace(session: Session) -> tuple[ActorBindingRow, ActorBindingRow]:
    stamp = now_utc()
    owner = ActorBindingRow(
        id=OWNER_BINDING_ID,
        tenant_id=DEFAULT_TENANT_ID,
        source="wwebjs",
        external_actor_id=OWNER_EXTERNAL_ID,
        actor_key=OWNER_ACTOR_ID,
        display_name="Alex",
        actor_category="owner",
        active_context=None,
        is_active=True,
        binding_metadata={"owner": True},
        created_at=stamp,
        updated_at=stamp,
    )
    peer = ActorBindingRow(
        id=PEER_BINDING_ID,
        tenant_id=DEFAULT_TENANT_ID,
        source="wwebjs",
        external_actor_id=PEER_EXTERNAL_ID,
        actor_key=PEER_ACTOR_ID,
        display_name="Peer",
        actor_category="family_core",
        active_context=None,
        is_active=True,
        binding_metadata={"audience": GRACE_AUDIENCE},
        created_at=stamp,
        updated_at=stamp,
    )
    session.add_all([owner, peer])
    _add_policy(session, _base_policy_config(), provision=True)
    create_relationship(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        source=EntityReference(entity_type="ACTOR", entity_id=PEER_ACTOR_ID),
        target=EntityReference(entity_type="ACTOR", entity_id=OWNER_ACTOR_ID),
        relationship_type="pai",
        metadata_sanitized={"authorization": "explicit_human_authorization"},
    )
    session.flush()
    return owner, peer


@pytest.fixture()
def owner_control_session(session: Session) -> Session:
    _install_owner_and_grace(session)
    return session


def _owner_event(event_id: str, text: str) -> NormalizedInboundEvent:
    stamp = datetime(2026, 9, 4, 18, 0, tzinfo=timezone.utc)
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
        },
    )


def _receipt(session: Session, event_id: str = "authority-receipt") -> InboundEventRow:
    event = _owner_event(event_id, "espera 60")
    return create_inbound_event(
        session,
        event.source,
        event.external_event_id,
        event.event_type,
        event.normalized_payload,
        event.correlation_id,
        event.tenant_id,
        event.received_at,
    )


def test_owner_control_signal_enforces_typed_parameters():
    stamp = now_utc()
    evidence = OwnerControlAuthorityEvidence("receipt", "binding", "mechanism")
    with pytest.raises(OwnerControlError, match="OWNER_CONTROL_PARAMETERS_INVALID"):
        OwnerControlSignal(
            signal_id="signal",
            tenant_id=DEFAULT_TENANT_ID,
            owner_actor_key=OWNER_ACTOR_ID,
            signal_kind=OwnerControlSignalKind.COMMAND,
            action=OwnerControlAction.SET_OWNER_REPLY_GRACE_SECONDS,
            parameters=GraceEnabledParameters(enabled=True),
            source_channel="wwebjs-owner-control",
            source_event_id="event",
            correlation_id="correlation",
            causation_id="receipt",
            occurred_at=stamp,
            received_at=stamp,
            authority_evidence=evidence,
        )


def test_authority_is_derived_from_canonical_owner(owner_control_session):
    owner = owner_control_session.get(ActorBindingRow, OWNER_BINDING_ID)
    receipt = _receipt(owner_control_session)
    authority, evidence = build_owner_operator_authority(
        owner_control_session,
        receipt=receipt,
        binding=owner,
    )
    assert authority.authenticated is True
    assert authority.roles == ["OWNER"]
    assert authority.operator_actor_id == OWNER_ACTOR_ID
    assert evidence.actor_binding_id == OWNER_BINDING_ID


@pytest.mark.parametrize(
    "missing_classification",
    ["from_me_classification", "final_from_me_classification"],
)
def test_authority_requires_both_transport_classifications(
    owner_control_session,
    missing_classification,
):
    owner = owner_control_session.get(ActorBindingRow, OWNER_BINDING_ID)
    receipt = _receipt(
        owner_control_session,
        f"missing-{missing_classification}",
    )
    payload = dict(receipt.payload)
    metadata = dict(payload["metadata"])
    metadata.pop(missing_classification)
    receipt.payload = {**payload, "metadata": metadata}
    with pytest.raises(OwnerControlError, match="OWNER_AUTHORITY_UNAVAILABLE"):
        build_owner_operator_authority(
            owner_control_session,
            receipt=receipt,
            binding=owner,
        )


def test_authority_wrong_binding_fails_closed(owner_control_session):
    peer = owner_control_session.get(ActorBindingRow, PEER_BINDING_ID)
    with pytest.raises(OwnerControlError, match="OWNER_AUTHORITY_UNAVAILABLE"):
        build_owner_operator_authority(
            owner_control_session,
            receipt=_receipt(owner_control_session, "wrong-binding"),
            binding=peer,
        )


def test_authority_inactive_binding_fails_closed(owner_control_session):
    owner = owner_control_session.get(ActorBindingRow, OWNER_BINDING_ID)
    owner.is_active = False
    with pytest.raises(OwnerControlError, match="OWNER_AUTHORITY_UNAVAILABLE"):
        build_owner_operator_authority(
            owner_control_session,
            receipt=_receipt(owner_control_session, "inactive-binding"),
            binding=owner,
        )


def test_authority_ambiguous_represented_owner_fails_closed(owner_control_session):
    stamp = now_utc()
    owner_control_session.add(
        ActorBindingRow(
            id="second-owner-binding",
            tenant_id=DEFAULT_TENANT_ID,
            source="test",
            external_actor_id="second-owner",
            actor_key="second-owner-actor",
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
    with pytest.raises(OwnerControlError, match="OWNER_AUTHORITY_UNAVAILABLE"):
        build_owner_operator_authority(
            owner_control_session,
            receipt=_receipt(owner_control_session, "ambiguous-owner"),
            binding=owner_control_session.get(ActorBindingRow, OWNER_BINDING_ID),
        )


def test_grace_policy_resolver_fails_when_none_are_enabled(session):
    with pytest.raises(OwnerControlError, match="OWNER_CONTROL_GRACE_POLICY_UNAVAILABLE"):
        resolve_owner_reply_grace_policy_id(session, tenant_id=DEFAULT_TENANT_ID)
    assert session.scalar(select(func.count()).select_from(OwnerOperationalControlRow)) == 0


def test_grace_policy_resolver_selects_exactly_one(owner_control_session):
    assert (
        resolve_owner_reply_grace_policy_id(
            owner_control_session,
            tenant_id=DEFAULT_TENANT_ID,
        )
        == GRACE_POLICY_ID
    )


def test_grace_policy_resolver_fails_when_multiple_are_enabled(owner_control_session):
    current = owner_control_session.get(
        PolicyVersionRow,
        owner_control_session.get(PolicyRow, GRACE_POLICY_ID).current_version_id,
    )
    second = dict(current.config)
    second["identifier"] = "second_owner_control_grace"
    second["name"] = "Second Owner Control Grace"
    _add_policy(owner_control_session, second)
    with pytest.raises(OwnerControlError, match="OWNER_CONTROL_GRACE_POLICY_AMBIGUOUS"):
        resolve_owner_reply_grace_policy_id(
            owner_control_session,
            tenant_id=DEFAULT_TENANT_ID,
        )
    assert (
        owner_control_session.scalar(
            select(func.count()).select_from(OwnerOperationalControlRow)
        )
        == 0
    )


@pytest.mark.parametrize(
    "text",
    [
        "espera 60 segundos",
        "espera 60",
        "coloque a espera em 60 segundos",
        "mude a espera para 60 segundos",
        "mude o grace para 60",
        "tempo de espera 60 segundos",
        "defina a espera em 60 segundos",
        "Andy, coloque a espera em 60 segundos!",
    ],
)
def test_seconds_parser_variants(text):
    parsed = parse_owner_grace_control(text)
    assert parsed.status == OwnerControlParseStatus.MATCHED
    assert parsed.action == OwnerControlAction.SET_OWNER_REPLY_GRACE_SECONDS
    assert parsed.parameters == GraceSecondsParameters(seconds=60)


@pytest.mark.parametrize(
    "text",
    [
        "ative as respostas automáticas",
        "retome as respostas automáticas",
        "ative a resposta automática",
    ],
)
def test_enable_parser_variants(text):
    parsed = parse_owner_grace_control(text)
    assert parsed.status == OwnerControlParseStatus.MATCHED
    assert parsed.action == OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED
    assert parsed.parameters == AutomaticResponsesEnabledParameters(enabled=True)


@pytest.mark.parametrize(
    "text",
    [
        "pause as respostas automáticas",
        "desative as respostas automáticas",
        "pause a resposta automática",
    ],
)
def test_disable_parser_variants(text):
    parsed = parse_owner_grace_control(text)
    assert parsed.status == OwnerControlParseStatus.MATCHED
    assert parsed.action == OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED
    assert parsed.parameters == AutomaticResponsesEnabledParameters(enabled=False)


@pytest.mark.parametrize(
    "text",
    [
        "acho que 30 segundos talvez seja pouco",
        "mude de assunto",
        "ligue a luz",
        "coloque o café na mesa",
        "espera aí",
        "espera um pouco",
        "espera que eu já vejo",
        "espera, já te respondo",
        "espera ele chegar",
        "espera só um minuto",
        "espera um segundo",
        "espera só um segundo",
        "espera um segundinho",
        "tempo de espera?",
        "desative a espera",
        "desligue a espera",
        "ative a espera",
        "ligue a espera",
    ],
)
def test_casual_owner_text_is_not_a_control_command(text):
    parsed = parse_owner_grace_control(text)
    assert parsed.status == OwnerControlParseStatus.NOT_CONTROL_COMMAND
    assert parsed.consumed is False


@pytest.mark.parametrize(
    "text",
    [
        "espera 60?",
        "espera 60 segundos?",
        "tempo de espera 60 segundos?",
        "pause as respostas automáticas?",
        "ative as respostas automáticas?",
        "retome as respostas automáticas?",
        "Andy, coloque a espera em 60 segundos?",
    ],
)
def test_explicit_questions_are_not_control_commands(text):
    parsed = parse_owner_grace_control(text)
    assert parsed.status == OwnerControlParseStatus.NOT_CONTROL_COMMAND
    assert parsed.consumed is False


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (
            "ative as respostas automáticas e desative as respostas automáticas",
            "CONTROL_COMMAND_AMBIGUOUS",
        ),
        ("mude a espera para 30 ou 60 segundos", "CONTROL_COMMAND_AMBIGUOUS"),
        ("espera 30,5 segundos", "CONTROL_COMMAND_INVALID_VALUE"),
        ("espera sessenta segundos", "CONTROL_COMMAND_INVALID_VALUE"),
    ],
)
def test_administrative_but_invalid_input_is_consumed(text, reason):
    parsed = parse_owner_grace_control(text)
    assert parsed.status == OwnerControlParseStatus.REJECTED
    assert parsed.reason_code == reason


def test_internal_ingress_requires_structural_owner_command_authentication():
    base = {
        "source": "wwebjs",
        "external_event_id": "owner-command-structural",
        "event_type": "message",
        "external_actor_id": OWNER_EXTERNAL_ID,
        "content": "espera 60",
        "event_origin": "OWNER_COMMAND",
        "owner_authenticated": True,
        "metadata": {
            "from_me": True,
            "owner_self_chat": True,
            "from_me_classification": "OWNER_COMMAND",
            "final_from_me_classification": "OWNER_COMMAND",
        },
    }
    assert InternalInboundPayload.model_validate(base).owner_authenticated is True
    for mutation in (
        {"owner_authenticated": False},
        {"metadata": {"from_me": False, "owner_self_chat": True}},
        {"metadata": {"from_me": True, "owner_self_chat": False}},
        {
            "metadata": {
                "from_me": True,
                "owner_self_chat": True,
                "from_me_classification": "OWNER_COMMAND",
            }
        },
        {
            "metadata": {
                "from_me": True,
                "owner_self_chat": True,
                "final_from_me_classification": "OWNER_COMMAND",
            }
        },
        {
            "metadata": {
                "from_me": True,
                "owner_self_chat": True,
                "from_me_classification": "OWNER_COMMAND",
                "final_from_me_classification": "UNKNOWN_FROM_ME",
            }
        },
    ):
        with pytest.raises(ValueError, match="authentication structure"):
            InternalInboundPayload.model_validate({**base, **mutation})
    with pytest.raises(ValueError, match="requires Owner Command"):
        InternalInboundPayload.model_validate(
            {
                **base,
                "event_origin": "EXTERNAL_INBOUND",
                "metadata": {"from_me": True},
            }
        )


def test_seconds_command_uses_technical_interaction_and_no_decision_path(owner_control_session):
    result = services.receive_normalized_inbound_event(
        owner_control_session,
        _owner_event("owner-command-seconds", "mude a espera para 60 segundos"),
    )
    interaction = owner_control_session.get(InteractionRow, result["id"])
    outbox = owner_control_session.scalar(select(OutboxMessageRow))
    control = owner_control_session.scalar(select(OwnerOperationalControlRow))
    receipt = owner_control_session.scalar(
        select(InboundEventRow).where(
            InboundEventRow.external_event_id == "owner-command-seconds"
        )
    )
    assert interaction.event_type == "OWNER_CONTROL_COMMAND"
    assert interaction.state == "COMPLETED"
    assert interaction.contact_id.startswith("owner_control:")
    assert interaction.contact_id != OWNER_ACTOR_ID
    assert interaction.inbound_text == "SET_OWNER_REPLY_GRACE_SECONDS seconds=60"
    assert receipt.interaction_id == interaction.id
    assert control.integer_value == 60
    assert outbox.action_type == "owner_control_text"
    assert outbox.destination == "local_transport"
    assert outbox.execution_intent_id is None
    assert outbox.payload["text"] == "Espera alterada para 60 segundos."
    assert owner_control_session.scalar(select(func.count()).select_from(QueueRow)) == 0
    assert owner_control_session.scalar(select(func.count()).select_from(DecisionRow)) == 0
    assert owner_control_session.scalar(select(func.count()).select_from(AgentDecisionRow)) == 0
    assert (
        owner_control_session.scalar(
            select(func.count()).select_from(ConversationResponseGraceWindowRow)
        )
        == 0
    )


@pytest.mark.parametrize(
    ("event_id", "text", "enabled", "confirmation"),
    [
        (
            "owner-command-disable-pause",
            "pause as respostas automáticas",
            False,
            "Andy pausada. Continuo recebendo mensagens, mas não vou responder automaticamente.",
        ),
        (
            "owner-command-disable",
            "desative as respostas automáticas",
            False,
            "Andy pausada. Continuo recebendo mensagens, mas não vou responder automaticamente.",
        ),
        (
            "owner-command-enable-resume",
            "retome as respostas automáticas",
            True,
            "Andy já está ativa.",
        ),
        (
            "owner-command-enable",
            "ative as respostas automáticas",
            True,
            "Andy já está ativa.",
        ),
    ],
)
def test_enabled_commands_dispatch_to_global_not_grace_domain(
    owner_control_session,
    event_id,
    text,
    enabled,
    confirmation,
):
    services.receive_normalized_inbound_event(owner_control_session, _owner_event(event_id, text))
    control = owner_control_session.scalar(select(OwnerAutomationControlRow))
    outbox = owner_control_session.scalar(select(OutboxMessageRow))
    assert control.automatic_responses_enabled is enabled
    assert owner_control_session.scalar(select(OwnerOperationalControlRow)) is None
    assert outbox.payload["text"] == confirmation


@pytest.mark.parametrize(
    ("event_id", "text"),
    [
        ("old-disable-wait", "desative a espera"),
        ("old-switch-off-wait", "desligue a espera"),
        ("old-enable-wait", "ative a espera"),
        ("old-switch-on-wait", "ligue a espera"),
    ],
)
def test_old_wait_enable_phrases_fall_through_without_control_mutation(
    owner_control_session,
    event_id,
    text,
):
    result = services.receive_normalized_inbound_event(
        owner_control_session,
        _owner_event(event_id, text),
    )
    interaction = owner_control_session.get(InteractionRow, result["id"])
    assert interaction.event_type == "message"
    assert owner_control_session.scalar(select(func.count()).select_from(QueueRow)) == 1
    assert owner_control_session.scalar(
        select(func.count()).select_from(OwnerOperationalControlRow)
    ) == 0
    assert owner_control_session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 0


@pytest.mark.parametrize(
    ("event_id", "text"),
    [
        ("owner-normal-chat", "acho que 30 segundos talvez seja pouco"),
        ("owner-wait-there", "espera aí"),
        ("owner-wait-a-bit", "espera um pouco"),
        ("owner-wait-see", "espera que eu já vejo"),
        ("owner-wait-reply", "espera, já te respondo"),
        ("owner-wait-arrive", "espera ele chegar"),
        ("owner-wait-minute", "espera só um minuto"),
        ("owner-wait-one-second", "espera um segundo"),
        ("owner-wait-just-one-second", "espera só um segundo"),
        ("owner-wait-diminutive", "espera um segundinho"),
        ("owner-wait-query", "tempo de espera?"),
        ("owner-wait-seconds-query", "espera 60?"),
    ],
)
def test_normal_owner_chat_falls_through_to_normal_conversation(
    owner_control_session,
    event_id,
    text,
):
    result = services.receive_normalized_inbound_event(
        owner_control_session,
        _owner_event(event_id, text),
    )
    interaction = owner_control_session.get(InteractionRow, result["id"])
    assert interaction.event_type == "message"
    assert interaction.contact_id == OWNER_ACTOR_ID
    assert owner_control_session.scalar(select(func.count()).select_from(QueueRow)) == 1
    assert owner_control_session.scalar(
        select(func.count()).select_from(OwnerOperationalControlRow)
    ) == 0
    assert owner_control_session.scalar(
        select(func.count()).select_from(OwnerOperationalControlChangeRow)
    ) == 0
    assert owner_control_session.scalar(
        select(func.count())
        .select_from(OutboxMessageRow)
        .where(OutboxMessageRow.action_type == "owner_control_text")
    ) == 0
    assert owner_control_session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 0


def test_seconds_command_is_independent_of_global_pause(owner_control_session):
    services.receive_normalized_inbound_event(
        owner_control_session,
        _owner_event("owner-pause-before-seconds", "pause as respostas automáticas"),
    )
    result = services.receive_normalized_inbound_event(
        owner_control_session,
        _owner_event("owner-seconds-while-paused", "espera 60"),
    )
    control = owner_control_session.scalar(select(OwnerOperationalControlRow))
    outbox = owner_control_session.scalar(
        select(OutboxMessageRow).where(OutboxMessageRow.interaction_id == result["id"])
    )
    assert control.enabled is True
    assert control.integer_value == 60
    assert owner_control_session.scalar(
        select(OwnerAutomationControlRow.automatic_responses_enabled)
    ) is False
    assert outbox.payload["text"] == "Espera alterada para 60 segundos."


def test_zero_seconds_command_remains_enabled_and_immediate(owner_control_session):
    services.receive_normalized_inbound_event(
        owner_control_session,
        _owner_event("owner-zero-seconds", "espera 0"),
    )
    control = owner_control_session.scalar(select(OwnerOperationalControlRow))
    outbox = owner_control_session.scalar(select(OutboxMessageRow))
    assert control.enabled is True
    assert control.integer_value == 0
    assert outbox.payload["text"] == "Espera alterada para 0 segundos."


@pytest.mark.parametrize(
    ("event_id", "text", "expected_fragment"),
    [
        ("owner-ambiguous", "mude a espera para 30 ou 60 segundos", "comando ambíguo"),
        ("owner-invalid", "espera 30,5 segundos", "valor inválido"),
        ("owner-out-of-policy", "espera 301 segundos", "fora do limite"),
    ],
)
def test_control_rejections_are_consumed_without_domain_mutation(
    owner_control_session,
    event_id,
    text,
    expected_fragment,
):
    result = services.receive_normalized_inbound_event(
        owner_control_session,
        _owner_event(event_id, text),
    )
    interaction = owner_control_session.get(InteractionRow, result["id"])
    outbox = owner_control_session.scalar(select(OutboxMessageRow))
    assert interaction.event_type == "OWNER_CONTROL_COMMAND"
    assert interaction.state == "COMPLETED"
    assert expected_fragment in outbox.payload["text"]
    assert owner_control_session.scalar(select(func.count()).select_from(QueueRow)) == 0
    assert owner_control_session.scalar(select(func.count()).select_from(OwnerOperationalControlRow)) == 0
    assert owner_control_session.scalar(select(func.count()).select_from(OwnerOperationalControlChangeRow)) == 0


def test_technical_interaction_is_absent_from_owner_conversation_history(owner_control_session):
    result = services.receive_normalized_inbound_event(
        owner_control_session,
        _owner_event("owner-isolated", "espera 60"),
    )
    current = InteractionRow(
        id=new_id(),
        tenant_id=DEFAULT_TENANT_ID,
        event_type="message",
        contact_id=OWNER_ACTOR_ID,
        contact_name="Alex",
        relationship_category="owner",
        active_context=None,
        inbound_text="Conversa normal",
        state="RECEIVED",
        correlation_id=new_id(),
        causation_id=None,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    owner_control_session.add(current)
    owner_control_session.flush()
    turns = _recent_agent_turns(
        owner_control_session,
        DEFAULT_TENANT_ID,
        OWNER_ACTOR_ID,
        current.id,
    )
    assert result["inbound_text"] not in {turn["content"] for turn in turns}


def test_command_replay_has_one_mutation_interaction_and_confirmation(owner_control_session):
    event = _owner_event("owner-command-replay", "espera 60")
    first = services.receive_normalized_inbound_event(owner_control_session, event)
    owner_control_session.commit()
    second = services.receive_normalized_inbound_event(owner_control_session, event)
    assert first["id"] == second["id"]
    assert owner_control_session.scalar(select(func.count()).select_from(InteractionRow)) == 1
    assert (
        owner_control_session.scalar(
            select(func.count()).select_from(OwnerOperationalControlChangeRow)
        )
        == 1
    )
    assert owner_control_session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 1
    assert owner_control_session.scalar(select(OwnerOperationalControlRow.revision)) == 1


def test_pre_fix_persisted_owner_receipt_cannot_mutate_control(owner_control_session):
    event = _owner_event("pre-fix-owner-command", "espera 60")
    payload = dict(event.normalized_payload)
    metadata = dict(payload["metadata"])
    metadata.pop("final_from_me_classification")
    payload["metadata"] = metadata
    receipt = create_inbound_event(
        owner_control_session,
        event.source,
        event.external_event_id,
        event.event_type,
        payload,
        event.correlation_id,
        event.tenant_id,
        event.received_at,
    )
    receipt.status = "PROCESSED"
    receipt.processed_at = now_utc()
    receipt_id = receipt.id
    owner_control_session.commit()

    result = services.receive_inbound_event(
        owner_control_session,
        source=event.source,
        external_event_id=event.external_event_id,
        event_type=event.event_type,
        contact_id=OWNER_ACTOR_ID,
        contact_name="Alex",
        relationship_category="owner",
        active_context=None,
        inbound_text=event.content,
        payload=payload,
        tenant_id=event.tenant_id,
        received_at=event.received_at,
        actor_binding=owner_control_session.get(ActorBindingRow, OWNER_BINDING_ID),
    )

    assert result == {
        "receipt_id": receipt_id,
        "status": "PROCESSED",
        "interaction_id": None,
    }
    assert owner_control_session.scalar(
        select(func.count()).select_from(OwnerOperationalControlRow)
    ) == 0
    assert owner_control_session.scalar(
        select(func.count()).select_from(OwnerOperationalControlChangeRow)
    ) == 0
    assert owner_control_session.scalar(select(func.count()).select_from(InteractionRow)) == 0
    assert owner_control_session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 0


class _LocalTransportRecorder:
    def __init__(self, exc: Exception | None = None):
        self.exc = exc
        self.calls: list[str] = []

    def dispatch_outbox(self, outbox):
        self.calls.append(outbox.idempotency_key)
        if self.exc:
            raise self.exc
        return type(
            "Result",
            (),
            {"status": "sent", "response": {"message_reference": "fixture"}},
        )()


def test_owner_control_outbox_uses_local_transport_without_execution_intent(
    owner_control_session,
    monkeypatch,
):
    result = services.receive_normalized_inbound_event(
        owner_control_session,
        _owner_event("owner-command-delivery", "espera 60"),
    )
    owner_control_session.commit()
    recorder = _LocalTransportRecorder()
    monkeypatch.setattr(services, "local_transport_outbound", recorder)
    monkeypatch.setattr(
        services.actions,
        "dispatch",
        lambda *_args, **_kwargs: pytest.fail("owner control fell through to mock actions"),
    )
    assert services.process_outbox(owner_control_session, "owner-control-worker") == 1
    outbox = owner_control_session.scalar(select(OutboxMessageRow))
    assert outbox.status == "DONE"
    assert outbox.execution_intent_id is None
    assert owner_control_session.scalar(select(func.count()).select_from(AgentExecutionIntentRow)) == 0
    assert recorder.calls == [f"owner-control:confirmation:{result['inbound_event_id']}"]
    assert owner_control_session.scalar(
        select(func.count()).select_from(AuditEventRow).where(
            AuditEventRow.event_type == "owner_control.confirmation_delivered"
        )
    ) == 1


def test_owner_control_ambiguous_delivery_is_not_blindly_replayed(
    owner_control_session,
    monkeypatch,
):
    services.receive_normalized_inbound_event(
        owner_control_session,
        _owner_event("owner-command-ambiguous-delivery", "espera 60"),
    )
    owner_control_session.commit()
    recorder = _LocalTransportRecorder(WwebjsOutboundError("network_timeout"))
    monkeypatch.setattr(services, "local_transport_outbound", recorder)
    assert services.process_outbox(owner_control_session, "first-worker") == 1
    outbox = owner_control_session.scalar(select(OutboxMessageRow))
    assert outbox.status == "AMBIGUOUS"
    assert services.process_outbox(owner_control_session, "second-worker") == 0
    assert len(recorder.calls) == 1


def test_automated_self_chat_observation_records_echo_suppression_once(session):
    stamp = now_utc()
    event = NormalizedInboundEvent(
        source="wwebjs",
        external_event_id="owner-control-echo",
        event_type="message",
        occurred_at=stamp,
        received_at=stamp,
        actor_id=OWNER_EXTERNAL_ID,
        actor_display_name="Router outbound observation",
        actor_category="owner_outbound_observation",
        channel="whatsapp",
        content="[owner outbound observation]",
        event_origin="ROUTER_AUTOMATED_OUTBOUND_OBSERVED",
        metadata={
            "from_me": True,
            "owner_self_chat": False,
            "authenticated_owner_self_chat_target": True,
            "from_me_classification": "ROUTER_AUTOMATED_OUTBOUND_OBSERVED",
            "final_from_me_classification": "ROUTER_AUTOMATED_OUTBOUND_OBSERVED",
        },
    )
    services.receive_normalized_inbound_event(session, event)
    session.commit()
    services.receive_normalized_inbound_event(session, event)
    assert session.scalar(
        select(func.count()).select_from(AuditEventRow).where(
            AuditEventRow.event_type == "owner_control.echo_suppressed"
        )
    ) == 1
