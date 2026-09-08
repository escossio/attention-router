from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from attention_router.adapters.wwebjs_owner_control import (
    canonical_command_text,
    parse_owner_grace_control,
)
from attention_router.application import autonomy, execution, services
from attention_router.application.owner_automation_control import (
    automation_denial_reason,
    set_automatic_responses_enabled,
)
from attention_router.application.owner_control import (
    AutomaticResponsesEnabledParameters,
    OwnerControlAction,
    OwnerControlError,
)
from attention_router.application.owner_operational_control import OperationalControlConflict
from attention_router.config import settings
from attention_router.core.events import OperatorAuthority, OwnerCommandUnauthorized
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import now_utc
from attention_router.infrastructure.models import (
    ActorBindingRow,
    AgentDecisionRow,
    AgentExecutionIntentRow,
    ConversationResponseGraceWindowRow,
    DecisionRow,
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
    OwnerAutomationControlChangeRow,
    OwnerAutomationControlRow,
    OwnerOperationalControlRow,
    PolicyRow,
    PolicyVersionRow,
    QueueRow,
)
from tests.test_autonomy import _fixture as automatic_decision
from tests.test_execution import _intent as human_intent
from tests.test_owner_control import (
    OWNER_ACTOR_ID,
    OWNER_BINDING_ID,
    _install_owner_and_grace,
    _owner_event,
    _LocalTransportRecorder,
    test_automated_self_chat_observation_records_echo_suppression_once as assert_echo_suppressed,
)


@pytest.fixture
def owner_session(session):
    _install_owner_and_grace(session)
    return session


def control(session, enabled, event_id, **overrides):
    arguments = dict(
        tenant_id=DEFAULT_TENANT_ID,
        represented_owner_actor_key=OWNER_ACTOR_ID,
        enabled=enabled,
        authority=OperatorAuthority(
            operator_actor_id=OWNER_ACTOR_ID,
            authenticated=True,
            roles=["OWNER"],
        ),
        source_channel="wwebjs-owner-control",
        source_event_id=event_id,
        provenance={"authentication_mechanism": "WWEBJS_AUTHENTICATED_SELF_CHAT"},
    )
    arguments.update(overrides)
    return set_automatic_responses_enabled(session, **arguments)


@pytest.mark.parametrize(
    "text,enabled",
    [
        ("pausa", False),
        ("pause", False),
        ("Andy, pausa", False),
        ("retoma", True),
        ("retome", True),
        ("Andy, retoma", True),
        ("pause as respostas automáticas", False),
        ("desative as respostas automáticas", False),
        ("retome as respostas automáticas", True),
        ("ative as respostas automáticas", True),
    ],
)
def test_global_parser_exact(text, enabled):
    parsed = parse_owner_grace_control(text)
    assert parsed.status == "MATCHED"
    assert parsed.action == OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED
    assert parsed.parameters == AutomaticResponsesEnabledParameters(enabled)
    assert canonical_command_text(parsed) == (
        f"SET_AUTOMATIC_RESPONSES_ENABLED enabled={str(enabled).lower()}"
    )


@pytest.mark.parametrize(
    "text",
    [
        "pausa?",
        "retoma?",
        "Andy, pausa?",
        "Andy, retoma?",
        "vou fazer uma pausa",
        "faz uma pausa aí",
        "pausa um pouco",
        "pausa isso",
        "retoma isso depois",
        "quando puder retoma",
        "continua",
        "para",
        "volta",
        "segura",
        "espera aí",
    ],
)
def test_global_parser_casual_and_questions(text):
    assert parse_owner_grace_control(text).status == "NOT_CONTROL_COMMAND"


@pytest.mark.parametrize(
    "text",
    [
        "pausa e retoma",
        "pause e retome",
        "ative e desative as respostas automáticas",
    ],
)
def test_global_parser_ambiguous(text):
    parsed = parse_owner_grace_control(text)
    assert parsed.status == "REJECTED"
    assert parsed.reason_code == "CONTROL_COMMAND_AMBIGUOUS"


@pytest.mark.parametrize("value", [None, 1, 0, "false"])
def test_boolean_parameter_strict(value):
    with pytest.raises(OwnerControlError):
        AutomaticResponsesEnabledParameters(value)


def test_default_revisions_noops_and_replay(owner_session):
    s = owner_session
    assert automation_denial_reason(s, DEFAULT_TENANT_ID) is None
    initial = control(s, True, "already-active")
    assert not initial.changed and initial.control.revision == 0
    paused = control(s, False, "pause")
    assert paused.changed and paused.control.revision == 1
    assert automation_denial_reason(s, DEFAULT_TENANT_ID) == "OWNER_AUTOMATION_PAUSED"
    assert not control(s, False, "pause-again").changed
    replay = control(s, False, "pause")
    assert replay.duplicate and not replay.changed and replay.control.revision == 1
    with pytest.raises(OperationalControlConflict):
        control(s, True, "pause")
    assert control(s, True, "resume").control.revision == 2
    assert not control(s, True, "resume-again").changed
    # Replaying an old pause must not reapply it after resume.
    assert control(s, False, "pause").duplicate
    assert automation_denial_reason(s, DEFAULT_TENANT_ID) is None
    assert s.scalar(select(func.count()).select_from(OwnerAutomationControlChangeRow)) == 5


@pytest.mark.parametrize(
    "override",
    [
        {"represented_owner_actor_key": "other-owner"},
        {"tenant_id": "wrong-tenant"},
        {"authority": OperatorAuthority(operator_actor_id=OWNER_ACTOR_ID, roles=["OWNER"])},
        {
            "authority": OperatorAuthority(
                operator_actor_id="other", authenticated=True, roles=["OWNER"]
            )
        },
        {"authority": OperatorAuthority(operator_actor_id=OWNER_ACTOR_ID, authenticated=True)},
    ],
)
def test_wrong_authority_is_rejected(owner_session, override):
    with pytest.raises(OwnerCommandUnauthorized):
        control(owner_session, False, "denied", **override)
    assert owner_session.scalar(select(OwnerAutomationControlRow)) is None


def test_session_reload_persists_and_owner_loss_does_not_bypass(owner_session):
    control(owner_session, False, "pause")
    owner_session.commit()
    with Session(owner_session.bind) as reloaded:
        assert automation_denial_reason(reloaded, DEFAULT_TENANT_ID) == "OWNER_AUTOMATION_PAUSED"
        reloaded.get(ActorBindingRow, OWNER_BINDING_ID).is_active = False
        reloaded.flush()
        assert (
            automation_denial_reason(reloaded, DEFAULT_TENANT_ID)
            == "OWNER_AUTOMATION_OWNER_UNRESOLVED"
        )


def enable_gates(monkeypatch):
    for name in (
        "agent_execution_enabled",
        "external_delivery_enabled",
        "autonomous_execution_enabled",
    ):
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr(
        settings,
        "autonomous_execution_activated_at",
        (now_utc() - timedelta(seconds=1)).isoformat(),
    )


def prepare_automatic(s, monkeypatch):
    enable_gates(monkeypatch)
    decision = automatic_decision(s)
    assert autonomy.evaluate_and_route(s, decision).automatic_execution_allowed
    assert execution.enqueue_ready_intents(s, transport_ready=True) == 1
    s.commit()
    return s.scalar(
        select(OutboxMessageRow).where(OutboxMessageRow.action_type == "agent_execution_text")
    )


def test_pause_is_global_across_actors_audiences_policies(owner_session, monkeypatch):
    enable_gates(monkeypatch)
    control(owner_session, False, "pause")
    for index, policy_id in enumerate(["desconhecido", "pai"]):
        assert owner_session.get(PolicyRow, policy_id) is not None
        decision = automatic_decision(owner_session, action="respond")
        decision.actor_id = f"actor-{index}"
        decision.audience = f"audience-{index}"
        policy = owner_session.get(PolicyVersionRow, decision.policy_version_id)
        policy.version = 100 + index
        policy.policy_id = policy_id
        policy.config = {**policy.config, "execution_scope": {"audiences": [decision.audience]}}
        row = autonomy.evaluate_and_route(owner_session, decision)
        assert not row.automatic_execution_allowed
        assert row.reason_code == "OWNER_AUTOMATION_PAUSED"
    assert owner_session.scalar(select(func.count()).select_from(AgentExecutionIntentRow)) == 0


def test_pause_between_evaluation_and_intent_creation(owner_session, monkeypatch):
    enable_gates(monkeypatch)
    original = autonomy.suppress_if_repeated

    def pause_during_evaluation(*args):
        control(owner_session, False, "race")
        return original(*args)

    monkeypatch.setattr(autonomy, "suppress_if_repeated", pause_during_evaluation)
    evaluation = autonomy.evaluate_and_route(owner_session, automatic_decision(owner_session))
    assert not evaluation.automatic_execution_allowed
    assert evaluation.reason_code == "OWNER_AUTOMATION_PAUSED"
    assert owner_session.scalar(select(AgentExecutionIntentRow)) is None


def test_pause_freezes_intent_without_destroying_it(owner_session, monkeypatch):
    enable_gates(monkeypatch)
    decision = automatic_decision(owner_session)
    autonomy.evaluate_and_route(owner_session, decision)
    control(owner_session, False, "pause")
    assert execution.enqueue_ready_intents(owner_session, transport_ready=True) == 0
    intent = owner_session.scalar(select(AgentExecutionIntentRow))
    assert intent.status == "READY" and intent.blocked_reason == "OWNER_AUTOMATION_PAUSED"
    control(owner_session, True, "resume")
    assert execution.enqueue_ready_intents(owner_session, transport_ready=True) == 1


@pytest.mark.parametrize("stale_after_resume", [False, True])
def test_pause_after_claim_before_send_and_normal_resume(
    owner_session, monkeypatch, stale_after_resume
):
    s = owner_session
    outbox = prepare_automatic(s, monkeypatch)
    recorder = _LocalTransportRecorder()
    monkeypatch.setattr(services, "local_transport_outbound", recorder)
    original = services.claim_outbox

    def claim_then_pause(*args):
        rows = original(*args)
        assert len(rows) == 1
        control(s, False, "pause-after-claim")
        s.commit()
        return rows

    monkeypatch.setattr(services, "claim_outbox", claim_then_pause)
    services.process_outbox(s)
    assert recorder.calls == []
    assert outbox.status == "PENDING" and outbox.last_error == "OWNER_AUTOMATION_PAUSED"
    assert outbox.attempt_count == 0
    monkeypatch.setattr(services, "claim_outbox", original)
    control(s, True, "resume")
    assert recorder.calls == []  # Resume itself never replays backlog.
    outbox.available_at = now_utc()
    if stale_after_resume:
        intent = s.get(AgentExecutionIntentRow, outbox.execution_intent_id)
        decision = s.get(AgentDecisionRow, intent.agent_decision_id)
        s.get(InboundEventRow, decision.event_id).received_at = now_utc() - timedelta(days=1)
    s.commit()
    services.process_outbox(s)
    assert len(recorder.calls) == (0 if stale_after_resume else 1)
    assert outbox.status == ("BLOCKED" if stale_after_resume else "DONE")
    services.process_outbox(s)
    assert len(recorder.calls) == (0 if stale_after_resume else 1)


def test_human_approved_intent_not_blocked_by_pause(owner_session, monkeypatch):
    enable_gates(monkeypatch)
    control(owner_session, False, "pause")
    intent = human_intent(owner_session)
    execution.release_intent(owner_session, intent.id, transport_ready=True)
    assert execution.enqueue_ready_intents(owner_session, transport_ready=True) == 1
    recorder = _LocalTransportRecorder()
    monkeypatch.setattr(services, "local_transport_outbound", recorder)
    services.process_outbox(owner_session)
    assert len(recorder.calls) == 1


def test_paused_backlog_does_not_starve_control_confirmation(owner_session, monkeypatch):
    s = owner_session
    automatic = prepare_automatic(s, monkeypatch)
    result = services.receive_normalized_inbound_event(
        s, _owner_event("pause-with-backlog", "pausa")
    )
    confirmation = s.scalar(
        select(OutboxMessageRow).where(OutboxMessageRow.interaction_id == result["id"])
    )
    s.commit()
    recorder = _LocalTransportRecorder()
    monkeypatch.setattr(services, "local_transport_outbound", recorder)
    assert services.process_outbox(s, limit=1) == 1
    assert recorder.calls == [confirmation.idempotency_key]
    assert automatic.status == "PENDING" and confirmation.status == "DONE"


def test_paused_ready_intents_do_not_starve_human_intent(owner_session, monkeypatch):
    s = owner_session
    enable_gates(monkeypatch)
    autonomy.evaluate_and_route(s, automatic_decision(s))
    control(s, False, "pause")
    human = human_intent(s)
    execution.release_intent(s, human.id, transport_ready=True)
    assert execution.enqueue_ready_intents(s, limit=1, transport_ready=True) == 1
    assert s.scalar(select(OutboxMessageRow.execution_intent_id)) == human.id


def test_owner_control_while_paused_confirmations_echo_and_grace(owner_session, monkeypatch):
    s = owner_session
    recorder = _LocalTransportRecorder()
    monkeypatch.setattr(services, "local_transport_outbound", recorder)
    commands = [
        ("espera 30", "Espera alterada para 30 segundos."),
        (
            "pausa",
            "Andy pausada. Continuo recebendo mensagens, mas não vou responder automaticamente.",
        ),
        ("pausa", "Andy já está pausada."),
        ("espera 45", "Espera alterada para 45 segundos."),
        ("espera 30", "Espera alterada para 30 segundos."),
        ("retoma", "Andy retomada. Respostas automáticas novamente ativas."),
        ("retoma", "Andy já está ativa."),
    ]
    for index, (text, confirmation) in enumerate(commands):
        result = services.receive_normalized_inbound_event(
            s, _owner_event(f"control-{index}", text)
        )
        outbox = s.scalar(
            select(OutboxMessageRow).where(OutboxMessageRow.interaction_id == result["id"])
        )
        assert outbox.payload["text"] == confirmation
        assert outbox.action_type == "owner_control_text" and outbox.execution_intent_id is None
        if text in {"pausa", "retoma"}:
            assert s.scalar(select(OwnerOperationalControlRow.integer_value)) == 30
        s.commit()
        services.process_outbox(s)
        assert outbox.status == "DONE"
        assert len(recorder.calls) == index + 1
    control(s, False, "pause-for-echo")
    assert_echo_suppressed(s)
    for model in (DecisionRow, AgentDecisionRow, QueueRow, AgentExecutionIntentRow):
        assert s.scalar(select(func.count()).select_from(model)) == 0
    assert s.scalar(select(func.count()).select_from(OwnerAutomationControlChangeRow)) == 5


def test_unauthenticated_pause_never_mutates(owner_session):
    event = _owner_event("no-authority", "pausa").model_copy(update={"owner_authenticated": False})
    services.receive_normalized_inbound_event(owner_session, event)
    assert owner_session.scalar(select(OwnerAutomationControlRow)) is None


def test_global_command_does_not_require_any_active_grace_policy(owner_session):
    for policy in owner_session.scalars(select(PolicyRow)):
        policy.is_active = False
    services.receive_normalized_inbound_event(
        owner_session, _owner_event("global-no-policy", "pausa")
    )
    assert automation_denial_reason(owner_session, DEFAULT_TENANT_ID) == "OWNER_AUTOMATION_PAUSED"
    assert owner_session.scalar(select(OwnerOperationalControlRow)) is None


def test_inbound_and_human_takeover_survive_pause(session):
    from tests.test_owner_reply_grace import (
        _install_canary,
        _inbound,
        _owner_observation,
        _receive_canary,
    )
    from attention_router.application.platform.context import resolve_represented_subject

    _install_canary(session)
    owner = resolve_represented_subject(session, DEFAULT_TENANT_ID).entity_id
    control(
        session,
        False,
        "pause",
        represented_owner_actor_key=owner,
        authority=OperatorAuthority(operator_actor_id=owner, authenticated=True, roles=["OWNER"]),
    )
    stamp = now_utc()
    result = _receive_canary(session, _inbound("external-while-paused", stamp))
    assert session.get(InteractionRow, result["id"]) is not None
    window = session.scalar(select(ConversationResponseGraceWindowRow))
    assert window.state == "OPEN" and window.effective_grace_seconds == 30
    _receive_canary(session, _owner_observation("manual-reply", stamp + timedelta(seconds=1)))
    assert window.state == "CANCELED"
    assert session.scalar(select(OutboxMessageRow)) is None


def test_grace_release_during_global_pause_cannot_dispatch(session, monkeypatch):
    from tests.test_owner_reply_grace import (
        _install_canary,
        _inbound,
        _receive_canary,
        _add_reversible_downstream,
    )
    from attention_router.application.owner_reply_grace import process_due_grace_windows
    from attention_router.application.platform.context import resolve_represented_subject

    _install_canary(session)
    owner = resolve_represented_subject(session, DEFAULT_TENANT_ID).entity_id
    control(
        session,
        False,
        "pause",
        represented_owner_actor_key=owner,
        authority=OperatorAuthority(operator_actor_id=owner, authenticated=True, roles=["OWNER"]),
    )
    stamp = now_utc()
    _receive_canary(session, _inbound("external-during-pause", stamp))
    assert (
        process_due_grace_windows(session, "worker", timestamp=stamp + timedelta(seconds=31)) == 1
    )
    window = session.scalar(select(ConversationResponseGraceWindowRow))
    assert window.state == "RELEASED" and window.effective_grace_seconds == 30
    intent, outbox = _add_reversible_downstream(session)
    session.commit()
    monkeypatch.setattr(
        services.local_transport_outbound,
        "dispatch_outbox",
        lambda _: pytest.fail("grace release must not bypass global pause"),
    )
    assert services.process_outbox(session) == 0
    assert intent.status == "QUEUED" and outbox.status == "PENDING"
    assert outbox.last_error == "OWNER_AUTOMATION_PAUSED"


def test_legacy_grace_enable_contract_is_not_global(owner_session):
    from attention_router.application.owner_control import (
        GraceEnabledParameters,
        build_owner_control_signal,
        build_owner_operator_authority,
        dispatch_owner_control_signal,
    )
    from tests.test_owner_control import _receipt

    s = owner_session
    receipt = _receipt(s, "internal-grace-only")
    binding = s.get(ActorBindingRow, OWNER_BINDING_ID)
    authority, evidence = build_owner_operator_authority(s, receipt=receipt, binding=binding)
    signal = build_owner_control_signal(
        receipt=receipt,
        binding=binding,
        action=OwnerControlAction.SET_OWNER_REPLY_GRACE_ENABLED,
        parameters=GraceEnabledParameters(False),
        authority_evidence=evidence,
    )
    result = dispatch_owner_control_signal(s, signal=signal, authority=authority)
    assert not result.mutation.control.enabled
    assert s.scalar(select(OwnerAutomationControlRow)) is None
    assert automation_denial_reason(s, DEFAULT_TENANT_ID) is None
