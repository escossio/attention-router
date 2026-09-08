from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select, func

from attention_router.adapters.inbound import NormalizedInboundEvent
from attention_router.application import services, autonomy, execution
from attention_router.application.direct_conversation import (
    direct_conversation_eligibility,
    conversational_policy_config,
)
from attention_router.application.decision_pipeline import process_agent_decision, _blueprint
from attention_router.application.agents.output import AndyAgentOutput
from attention_router.application.agents.service import AndyAgentResult
from attention_router.application.owner_reply_grace import process_due_grace_windows
from attention_router.application.platform.disclosure import (
    evaluate_disclosure_authority,
    project_private_state_for_agent,
)
from attention_router.config import settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import now_utc, new_id
from attention_router.infrastructure.models import (
    ActorBindingRow,
    AgentBlueprintRow,
    AgentBlueprintVersionRow,
    ConversationResponseGraceWindowRow,
    AgentExecutionIntentRow,
    OwnerOperationalControlRow,
    PolicyVersionRow,
    InboundEventRow,
    OutboxMessageRow,
    EntityStateRow,
    OwnerAutomationControlRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding, ensure_policy_version

PEER = "5500000000026@lid"


def direct_event(peer=PEER, **changes):
    data = dict(
        source="wwebjs",
        external_event_id=new_id(),
        event_type="message",
        actor_id=peer,
        actor_display_name="Unknown",
        actor_category="unknown",
        channel="whatsapp",
        content="Explique fibra óptica em uma frase.",
        event_origin="EXTERNAL_INBOUND",
        owner_authenticated=False,
        metadata={
            "from_me": False,
            "source_account": "default",
            "conversation_state": "READY",
            "conversation_key": "wwebjs:" + peer,
            "peer_identifiers": [peer],
        },
    )
    data.update(changes)
    return NormalizedInboundEvent(**data)


def row_event(event=None):
    event = event or direct_event()
    return SimpleNamespace(
        source=event.source,
        event_type=event.event_type,
        payload=event.normalized_payload,
        lineage_classification=event.lineage_classification,
    )


@pytest.mark.parametrize("peer", [PEER, "5500000000027@c.us"])
def test_direct_peer(peer):
    result = direct_conversation_eligibility(row_event(direct_event(peer)))
    assert result.eligible and result.return_channel_available


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "meta"),
        ("channel", "synthetic"),
        ("event_type", "call"),
        ("event_origin", "OWNER_COMMAND"),
        ("event_origin", "OWNER_MANUAL_OUTBOUND_OBSERVED"),
        ("owner_authenticated", True),
        ("lineage_classification", "SYNTHETIC"),
        ("lineage_classification", "HISTORICAL_UNKNOWN"),
        ("metadata.from_me", True),
        ("metadata.from_me", None),
        ("metadata.conversation_state", "AMBIGUOUS"),
        ("metadata.conversation_key", None),
        ("metadata.source_account", None),
        ("metadata.is_group", True),
        ("actor_id", "contact003@example.com"),
        ("actor_id", "status@broadcast"),
        ("actor_id", "123@newsletter"),
        ("actor_id", "123@broadcast"),
        ("actor_id", "garbage"),
    ],
)
def test_direct_negative(field, value):
    row = row_event()
    if field in {"source", "lineage_classification"}:
        setattr(row, field, value)
    elif field.startswith("metadata."):
        row.payload["metadata"][field.split(".")[1]] = value
    else:
        row.payload[field] = value
    assert not direct_conversation_eligibility(row).eligible


def test_baseline_composes_without_mutating_capabilities():
    legacy = {
        "allowed_actions": ["soft_ping"],
        "allowed_disclosures": [],
        "allowed_capabilities": [],
    }
    before = deepcopy(legacy)
    assert conversational_policy_config(legacy)["execution_mode"] == "AUTO_ALLOWED"
    assert legacy == before
    explicit = {"execution_mode": "REQUIRES_APPROVAL", "allowed_actions": ["respond"]}
    assert conversational_policy_config(explicit) == explicit
    assert (
        conversational_policy_config({"conversational_execution": False})["execution_mode"]
        == "OBSERVE"
    )


def setup_direct(session, monkeypatch):
    now = now_utc()
    upsert_actor_binding(
        session,
        source="wwebjs",
        external_actor_id="owner-test@c.us",
        actor_key="owner_direct_test",
        actor_category="owner",
        display_name="Owner",
        metadata={"owner": True},
    )
    bp = AgentBlueprintRow(
        id=new_id(),
        name="Explicit conversation blueprint",
        status="published",
        created_at=now,
        updated_at=now,
    )
    session.add(bp)
    session.flush()
    v = AgentBlueprintVersionRow(
        id=new_id(),
        blueprint_id=bp.id,
        version=1,
        spec={"autonomy": {"execution_mode": "AUTO_ALLOWED"}},
        checksum=new_id(),
        created_at=now,
        created_by="test",
        change_reason="test",
        is_immutable=True,
    )
    session.add(v)
    bp.current_version_id = v.id
    session.add(
        OwnerOperationalControlRow(
            id=new_id(),
            tenant_id=DEFAULT_TENANT_ID,
            represented_owner_actor_key="owner_direct_test",
            control_key="owner_reply_grace",
            scope_type="GLOBAL",
            policy_id=None,
            enabled=True,
            integer_value=10,
            revision=3,
            updated_by_actor_key="owner_direct_test",
            authorization_source="TEST",
            source_channel="test",
            provenance={},
            created_at=now,
            updated_at=now,
        )
    )
    for key, value in {
        "agent_decision_default_blueprint_id": bp.id,
        "andy_agent_enabled": True,
        "agent_decision_pipeline_enabled": True,
        "autonomous_execution_enabled": True,
        "external_delivery_enabled": True,
        "agent_execution_enabled": True,
        "autonomous_execution_activated_at": (now - timedelta(seconds=30)).isoformat(),
    }.items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(
        "attention_router.application.decision_pipeline.get_andy_readiness",
        lambda: SimpleNamespace(state="READY"),
    )
    captured = []

    def agent(context):
        captured.append(context)
        return AndyAgentResult(
            AndyAgentOutput(
                response_text="Fibra óptica transmite dados por luz.",
                intent="general",
                objective="answer",
                reason_code="TEST",
                confidence="high",
                conversation_state="answer",
            ),
            1,
            1,
            0,
        )

    monkeypatch.setattr("attention_router.application.decision_pipeline.run_andy", agent)
    session.flush()
    return captured


def test_unknown_full_pipeline_without_binding(session, monkeypatch):
    captured = setup_direct(session, monkeypatch)
    before = session.scalar(select(func.count()).select_from(ActorBindingRow))
    accepted = services.receive_normalized_inbound_event(session, direct_event())
    window = session.scalar(select(ConversationResponseGraceWindowRow))
    assert window and window.actor_binding_id is None and window.effective_grace_seconds == 10
    assert window.operational_control_revision == 3
    process_due_grace_windows(session, "test", timestamp=window.due_at + timedelta(seconds=1))
    decision = process_agent_decision(session, accepted["inbound_event_id"])
    assert session.get(PolicyVersionRow, decision.policy_version_id).policy_id == "desconhecido"
    assert captured and captured[0].whatsapp_return_channel_available
    prompt = json.dumps(captured[0].prompt_payload())
    for raw in (PEER, PEER.split("@")[0], "@c.us", "@lid", "wwebjs:"):
        assert raw not in prompt
    result = autonomy.evaluate_and_route(session, decision)
    assert result.automatic_execution_allowed, result.reason_code
    intent = session.scalar(select(AgentExecutionIntentRow))
    assert intent and execution.resolve_recipient(session, intent).reference == PEER
    assert execution.automatic_intent_denial_reason(session, intent) is None
    assert execution.enqueue_ready_intents(session, transport_ready=True) == 1
    assert execution.enqueue_ready_intents(session, transport_ready=True) == 0
    outbox = session.scalar(select(OutboxMessageRow))
    assert outbox.action_type == "agent_execution_text" and outbox.execution_intent_id == intent.id
    assert session.scalar(select(func.count()).select_from(ActorBindingRow)) == before
    assert captured[0].allowed_disclosures == []


@pytest.mark.parametrize(
    "peer", ["contact003@example.com", "status@broadcast", "123@newsletter", "123@broadcast"]
)
def test_non_direct_never_calls_agent_or_creates_effect(session, monkeypatch, peer):
    captured = setup_direct(session, monkeypatch)
    received = services.receive_normalized_inbound_event(session, direct_event(peer))
    decision = process_agent_decision(session, received["inbound_event_id"])
    evaluation = autonomy.evaluate_and_route(session, decision)
    assert not captured and not evaluation.automatic_execution_allowed
    assert not session.scalar(select(AgentExecutionIntentRow))
    assert not session.scalar(select(OutboxMessageRow))


@pytest.mark.parametrize("lineage", ["SYNTHETIC", "HISTORICAL_UNKNOWN"])
def test_non_organic_no_agent_no_intent(session, monkeypatch, lineage):
    captured = setup_direct(session, monkeypatch)
    received = services.receive_normalized_inbound_event(session, direct_event())
    receipt = session.get(InboundEventRow, received["inbound_event_id"])
    receipt.lineage_classification = lineage
    session.flush()
    decision = process_agent_decision(session, receipt.id)
    evaluation = autonomy.evaluate_and_route(session, decision)
    assert not captured and not evaluation.automatic_execution_allowed
    assert not session.scalar(select(AgentExecutionIntentRow))


def owner_event(text):
    return direct_event(
        "owner-test@c.us",
        content=text,
        event_origin="OWNER_COMMAND",
        owner_authenticated=True,
        metadata={
            "from_me": True,
            "owner_self_chat": True,
            "authenticated_owner_self_chat_target": True,
            "source_account": "default",
            "from_me_classification": "OWNER_COMMAND",
            "final_from_me_classification": "OWNER_COMMAND",
        },
    )


def test_wait_is_global_and_does_not_change_policy(session, monkeypatch):
    setup_direct(session, monkeypatch)
    before = [
        (p.id, p.checksum, deepcopy(p.config)) for p in session.scalars(select(PolicyVersionRow))
    ]
    command = owner_event("espera 15")
    services.receive_normalized_inbound_event(session, command)
    control = session.scalar(select(OwnerOperationalControlRow))
    assert control.scope_type == "GLOBAL" and control.policy_id is None
    assert control.integer_value == 15 and control.revision == 4
    session.commit()
    services.receive_normalized_inbound_event(session, command)
    assert control.revision == 4
    services.receive_normalized_inbound_event(session, owner_event("espera 10"))
    assert control.integer_value == 10 and control.revision == 5
    assert before == [
        (p.id, p.checksum, p.config) for p in session.scalars(select(PolicyVersionRow))
    ]


def test_unknown_private_state_never_reaches_agent(session, monkeypatch):
    captured = setup_direct(session, monkeypatch)
    now = now_utc()
    session.add(
        EntityStateRow(
            id=new_id(),
            tenant_id=DEFAULT_TENANT_ID,
            subject_type="ACTOR",
            subject_id="owner_direct_test",
            state_namespace="presence",
            state_key="effective",
            state_value={
                "status": "private-secret-state",
                "coordinates": "private-secret-location",
                "audience_scope": "all",
            },
            source="test",
            effective_at=now,
            version=1,
            updated_at=now,
        )
    )
    received = services.receive_normalized_inbound_event(
        session, direct_event(content="Onde o Alex está?")
    )
    process_agent_decision(session, received["inbound_event_id"])
    assert captured[0].current_operational_state == []
    assert "private-secret" not in json.dumps(captured[0].prompt_payload())


def test_manual_pn_reply_cancels_unbound_lid_window(session, monkeypatch):
    setup_direct(session, monkeypatch)
    services.receive_normalized_inbound_event(session, direct_event())
    window = session.scalar(select(ConversationResponseGraceWindowRow))
    observation = direct_event(
        "5500000000027@c.us",
        content="[owner outbound observation]",
        occurred_at=window.last_inbound_at + timedelta(seconds=1),
        received_at=window.last_inbound_at + timedelta(seconds=1),
        event_origin="OWNER_MANUAL_OUTBOUND_OBSERVED",
        metadata={
            "from_me": True,
            "source_account": "default",
            "conversation_state": "READY",
            "conversation_key": "wwebjs:" + PEER,
            "peer_identifiers": [PEER, "5500000000027@c.us"],
            "from_me_classification": "OWNER_MANUAL_OUTBOUND_OBSERVED",
        },
    )
    services.receive_normalized_inbound_event(session, observation)
    assert window.state == "CANCELED" and window.cancellation_reason == "OWNER_MANUAL_REPLY"
    assert (
        process_due_grace_windows(session, "test", timestamp=window.due_at + timedelta(seconds=1))
        == 0
    )
    assert not session.scalar(select(AgentExecutionIntentRow))


def test_pause_and_late_structural_invalidation_deny_last_mile(session, monkeypatch):
    setup_direct(session, monkeypatch)
    received = services.receive_normalized_inbound_event(session, direct_event())
    decision = process_agent_decision(session, received["inbound_event_id"])
    evaluation = autonomy.evaluate_and_route(session, decision)
    assert evaluation.automatic_execution_allowed
    intent = session.scalar(select(AgentExecutionIntentRow))
    services.receive_normalized_inbound_event(session, owner_event("pausa"))
    assert execution.automatic_intent_denial_reason(session, intent) == "OWNER_AUTOMATION_PAUSED"
    assert execution.enqueue_ready_intents(session, transport_ready=True) == 0
    assert session.scalar(select(OwnerAutomationControlRow)).automatic_responses_enabled is False
    # Owner Control, including Grace and confirmations, remains available while paused.
    services.receive_normalized_inbound_event(session, owner_event("espera 15"))
    services.receive_normalized_inbound_event(session, owner_event("retoma"))
    assert session.scalar(select(OwnerAutomationControlRow)).automatic_responses_enabled is True
    assert all(
        o.action_type == "owner_control_text" for o in session.scalars(select(OutboxMessageRow))
    )
    receipt = session.get(InboundEventRow, received["inbound_event_id"])
    receipt.payload = {
        **receipt.payload,
        "metadata": {**receipt.payload["metadata"], "conversation_state": "AMBIGUOUS"},
    }
    session.flush()
    assert (
        execution.automatic_intent_denial_reason(session, intent)
        == "DIRECT_CONVERSATION_UNRESOLVED"
    )


def test_no_blueprint_ranking_for_direct(session, monkeypatch):
    captured = setup_direct(session, monkeypatch)
    monkeypatch.setattr(settings, "agent_decision_default_blueprint_id", None)
    assert _blueprint(session, None, DEFAULT_TENANT_ID, explicit_only=True) == (None, None)
    received = services.receive_normalized_inbound_event(session, direct_event())
    decision = process_agent_decision(session, received["inbound_event_id"])
    assert (
        not captured
        and not autonomy.evaluate_and_route(session, decision).automatic_execution_allowed
    )
    assert not session.scalar(select(AgentExecutionIntentRow))


def test_private_projection_is_authority_enforced():
    states = [
        {
            "namespace": "presence",
            "key": "effective",
            "value": {
                "status": "sleeping",
                "audience_scope": "all",
                "coordinates": "secret-location",
            },
            "version": 1,
        }
    ]
    denied = evaluate_disclosure_authority(
        directives=[], allowed_disclosures=[], operational_state=states
    )
    assert project_private_state_for_agent(denied, states) == []
    allowed = evaluate_disclosure_authority(
        directives=[SimpleNamespace(effect_type="DISCLOSE_CURRENT_PRESENCE")],
        allowed_disclosures=["availability_hint"],
        operational_state=states,
    )
    projection = project_private_state_for_agent(allowed, states)
    assert projection[0]["value"] == {"status": "sleeping", "audience_scope": "all"}
    assert "secret-location" not in json.dumps(projection)


def test_location_stays_unavailable_without_grants(session):
    from attention_router.application.platform.registry import (
        sync_platform_registry,
        resolve_capability_request,
    )
    from attention_router.core.capabilities import CapabilityRequest
    from attention_router.infrastructure.models import CapabilityGrantRow

    sync_platform_registry(session)
    result = resolve_capability_request(
        session,
        CapabilityRequest(capability="location.current"),
        tenant_id=DEFAULT_TENANT_ID,
        grantee_type="ACTOR",
        grantee_id="unknown",
        policy_allows=False,
    )
    assert result.status.value == "KNOWN_BUT_UNAVAILABLE"
    assert session.scalar(select(func.count()).select_from(CapabilityGrantRow)) == 0


def test_authorized_policy_directive_presence_projection_in_pipeline(session, monkeypatch):
    captured = setup_direct(session, monkeypatch)
    upsert_actor_binding(
        session,
        source="wwebjs",
        external_actor_id=PEER,
        actor_key="contact_mae",
        actor_category="family_core",
        display_name="Family fixture",
    )
    now = now_utc()
    session.add(
        EntityStateRow(
            id=new_id(),
            tenant_id=DEFAULT_TENANT_ID,
            subject_type="ACTOR",
            subject_id="owner_direct_test",
            state_namespace="presence",
            state_key="effective",
            state_value={
                "status": "sleeping",
                "audience_scope": "all",
                "private_note": "NEVER_PROJECT",
            },
            source="test",
            effective_at=now,
            version=1,
            updated_at=now,
        )
    )
    monkeypatch.setattr(
        "attention_router.application.decision_pipeline.resolve_effective_standing_directives",
        lambda *a, **kw: [
            SimpleNamespace(
                id="directive",
                trigger_type="INBOUND_MESSAGE",
                effect_type="DISCLOSE_CURRENT_PRESENCE",
                audience_selector={"type": "all"},
            )
        ],
    )
    received = services.receive_normalized_inbound_event(session, direct_event())
    process_agent_decision(session, received["inbound_event_id"])
    assert captured[0].current_operational_state == [
        {
            "namespace": "presence",
            "key": "effective",
            "value": {"status": "sleeping", "audience_scope": "all"},
        }
    ]
    assert "NEVER_PROJECT" not in json.dumps(captured[0].prompt_payload())


def test_self_is_observed_without_autonomous_conversation(session, monkeypatch):
    captured = setup_direct(session, monkeypatch)
    event = direct_event()
    event.metadata = {**event.metadata, "from_me": True}
    received = services.receive_normalized_inbound_event(session, event)
    assert received["interaction_id"] is None
    assert not captured and not session.scalar(select(AgentExecutionIntentRow))


def test_morgan_v4_fixture_stays_selected_and_immutable(session, monkeypatch):
    from tests.test_owner_reply_grace import _policy_config, CANARY_BINDING_ID, CANARY_ACTOR_ID
    from attention_router.infrastructure.models import PolicyRow
    from attention_router.infrastructure.hashing import stable_hash
    from attention_router.application.platform.entities import create_relationship
    from attention_router.core.entities import EntityReference

    captured = setup_direct(session, monkeypatch)
    stamp = now_utc()
    session.add(
        ActorBindingRow(
            id=CANARY_BINDING_ID,
            tenant_id=DEFAULT_TENANT_ID,
            source="wwebjs",
            external_actor_id=PEER,
            actor_key=CANARY_ACTOR_ID,
            display_name="Family fixture",
            actor_category="family_core",
            is_active=True,
            binding_metadata={"audience": "autonomy_canary"},
            created_at=stamp,
            updated_at=stamp,
        )
    )
    config = _policy_config()
    config.update(
        allowed_actions=["respond", "request_information"],
        owner_reply_grace_allowed=True,
        owner_reply_grace_default_seconds=30,
        owner_reply_grace_min_seconds=0,
        owner_reply_grace_max_seconds=300,
        owner_reply_grace_mode="TRAILING_EDGE",
    )
    policy = PolicyRow(
        **{k: v for k, v in config.items() if k in PolicyRow.__table__.columns},
        tenant_id=DEFAULT_TENANT_ID,
        is_active=True,
    )
    session.add(policy)
    session.flush()
    version = PolicyVersionRow(
        id=new_id(),
        policy_id=policy.identifier,
        version=4,
        config=deepcopy(config),
        checksum=stable_hash(config),
        created_at=stamp,
        created_by="test",
        is_immutable=True,
        status="ACTIVE",
    )
    session.add(version)
    policy.current_version_id = version.id
    create_relationship(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        source=EntityReference(entity_type="ACTOR", entity_id=CANARY_ACTOR_ID),
        target=EntityReference(entity_type="ACTOR", entity_id="owner_direct_test"),
        relationship_type="pai",
        metadata_sanitized={},
    )
    received = services.receive_normalized_inbound_event(session, direct_event())
    decision = process_agent_decision(session, received["inbound_event_id"])
    assert decision.policy_version_id == version.id
    assert captured and autonomy.evaluate_and_route(session, decision).automatic_execution_allowed
    assert (
        version.config == config
        and version.checksum == stable_hash(config)
        and version.version == 4
    )


@pytest.mark.parametrize(
    "policy_id,actor,category,active",
    [
        ("mae", "contact_mae", "family_core", None),
        ("pai", "contact_pai", "family_core", None),
        ("recrutador", "contact_recruiter", "professional", "interview"),
        ("desconhecido", None, "unknown", None),
    ],
)
def test_specific_policy_preserved_with_text_baseline(
    session, monkeypatch, policy_id, actor, category, active
):
    captured = setup_direct(session, monkeypatch)
    if actor:
        upsert_actor_binding(
            session,
            source="wwebjs",
            external_actor_id=PEER,
            actor_key=actor,
            actor_category=category,
            display_name="Fixture",
            active_context=active,
        )
    received = services.receive_normalized_inbound_event(session, direct_event())
    decision = process_agent_decision(session, received["inbound_event_id"])
    policy = session.get(PolicyVersionRow, decision.policy_version_id)
    assert policy.policy_id == policy_id
    assert captured[0].allowed_disclosures == policy.config["allowed_disclosures"]
    assert autonomy.evaluate_and_route(session, decision).automatic_execution_allowed


def test_explicit_text_policy_restriction_is_not_overridden(session, monkeypatch):
    setup_direct(session, monkeypatch)
    from attention_router.infrastructure.models import PolicyRow

    policy = session.get(PolicyRow, "desconhecido")
    config = deepcopy(session.get(PolicyVersionRow, policy.current_version_id).config)
    config["conversational_execution"] = {
        "execution_mode": "OBSERVE",
        "allowed_actions": ["respond"],
    }
    ensure_policy_version(session, policy, config, "test")
    received = services.receive_normalized_inbound_event(session, direct_event())
    decision = process_agent_decision(session, received["inbound_event_id"])
    evaluation = autonomy.evaluate_and_route(session, decision)
    assert not evaluation.automatic_execution_allowed and evaluation.reason_code == "OBSERVE_ONLY"
    assert not session.scalar(select(AgentExecutionIntentRow))


def test_last_mile_rejects_changed_outbox_target_before_adapter(session, monkeypatch):
    from tests.test_owner_control import _LocalTransportRecorder

    setup_direct(session, monkeypatch)
    received = services.receive_normalized_inbound_event(session, direct_event())
    window = session.scalar(select(ConversationResponseGraceWindowRow))
    assert (
        process_due_grace_windows(session, "test", timestamp=window.due_at + timedelta(seconds=1))
        == 1
    )
    decision = process_agent_decision(session, received["inbound_event_id"])
    assert autonomy.evaluate_and_route(session, decision).automatic_execution_allowed
    execution.enqueue_ready_intents(session, transport_ready=True)
    outbox = session.scalar(select(OutboxMessageRow))
    outbox.payload = {**outbox.payload, "external_actor_id": "5500000000028@c.us"}
    session.commit()
    recorder = _LocalTransportRecorder()
    monkeypatch.setattr(services, "local_transport_outbound", recorder)
    services.process_outbox(session, "test")
    assert outbox.last_error == "DIRECT_OUTBOX_CONTRACT_MISMATCH"
    assert not recorder.calls


@pytest.mark.parametrize("auto_policy", [False, True], ids=["unknown", "morgan-v4-like"])
def test_agent_failure_never_falls_back(session, monkeypatch, auto_policy):
    from attention_router.application.agents import AndyAgentError
    from attention_router.application import decision_pipeline as pipeline

    setup_direct(session, monkeypatch)
    received = services.receive_normalized_inbound_event(session, direct_event())
    if auto_policy:
        policy = session.scalar(
            select(PolicyVersionRow).where(PolicyVersionRow.policy_id == "desconhecido")
        )
        policy.config = {
            **policy.config,
            "execution_mode": "AUTO_ALLOWED",
            "allowed_actions": ["respond", "request_information"],
        }
        session.flush()
    calls = []
    original_gate = pipeline._agent_enabled_for

    def gate(*args, **kwargs):
        calls.append(kwargs)
        return original_gate(*args, **kwargs)

    def fail(*args, **kwargs):
        raise AndyAgentError("TEST_AGENT_UNAVAILABLE")

    def forbidden(*args, **kwargs):
        pytest.fail("Agent failure entered deterministic/behavior fallback")

    monkeypatch.setattr(pipeline, "_agent_enabled_for", gate)
    monkeypatch.setattr(pipeline, "run_andy", fail)
    monkeypatch.setattr(pipeline.DecisionEngine, "decide", forbidden)
    monkeypatch.setattr(pipeline.ResponseGenerator, "propose", forbidden)
    monkeypatch.setattr(pipeline, "render_response", forbidden)
    monkeypatch.setattr(settings, "legacy_external_fallback_enabled", True)
    decision = process_agent_decision(session, received["inbound_event_id"])
    assert len(calls) == 1 and calls[0]["blueprint_configured"]
    assert decision.recommended_action == "observe" and decision.proposed_response is None
    assert not autonomy.evaluate_and_route(session, decision).automatic_execution_allowed
    assert session.scalar(select(AgentExecutionIntentRow)) is None
    assert session.scalar(select(OutboxMessageRow)) is None


@pytest.mark.parametrize(
    "state,text", [("hold", ""), ("clarify", ""), ("action_requested", ""), ("clarify", " \t\n")]
)
def test_agent_empty_response_cannot_become_automatic_effect(session, monkeypatch, state, text):
    from attention_router.application.agents.service import validate_output

    setup_direct(session, monkeypatch)
    output = validate_output(
        AndyAgentOutput(
            response_text=text,
            intent="general",
            objective="answer",
            reason_code="TEST",
            confidence="high",
            conversation_state=state,
        )
    )
    monkeypatch.setattr(
        "attention_router.application.decision_pipeline.run_andy",
        lambda context: AndyAgentResult(output, 1, 1, 0),
    )
    received = services.receive_normalized_inbound_event(session, direct_event())
    decision = process_agent_decision(session, received["inbound_event_id"])
    assert decision.recommended_action == "observe" and decision.proposed_response is None
    evaluation = autonomy.evaluate_and_route(session, decision)
    assert not evaluation.automatic_execution_allowed
    assert evaluation.reason_code == "AUTOMATIC_RESPONSE_EMPTY"
    assert session.scalar(select(AgentExecutionIntentRow)) is None
    assert session.scalar(select(OutboxMessageRow)) is None


@pytest.mark.parametrize("text", [None, "", " \t\n"])
def test_autonomy_rejects_preexisting_empty_respond_decision(session, monkeypatch, text):
    setup_direct(session, monkeypatch)
    received = services.receive_normalized_inbound_event(session, direct_event())
    decision = process_agent_decision(session, received["inbound_event_id"])
    decision.proposed_response = text
    evaluation = autonomy.evaluate_and_route(session, decision)
    assert (
        not evaluation.automatic_execution_allowed
        and evaluation.reason_code == "AUTOMATIC_RESPONSE_EMPTY"
    )
    assert session.scalar(select(AgentExecutionIntentRow)) is None


@pytest.mark.parametrize("text", ["", " \t\n"])
def test_empty_prematerialized_intent_denied_before_network(session, monkeypatch, text):
    from tests.test_owner_control import _LocalTransportRecorder

    setup_direct(session, monkeypatch)
    received = services.receive_normalized_inbound_event(session, direct_event())
    window = session.scalar(select(ConversationResponseGraceWindowRow))
    assert (
        process_due_grace_windows(session, "test", timestamp=window.due_at + timedelta(seconds=1))
        == 1
    )
    decision = process_agent_decision(session, received["inbound_event_id"])
    assert autonomy.evaluate_and_route(session, decision).automatic_execution_allowed
    assert execution.enqueue_ready_intents(session, transport_ready=True) == 1
    intent = session.scalar(select(AgentExecutionIntentRow))
    outbox = session.scalar(select(OutboxMessageRow))
    intent.effective_response_snapshot = text
    outbox.payload = {**outbox.payload, "text": text}
    session.commit()
    assert execution.automatic_intent_denial_reason(session, intent) == "AUTOMATIC_RESPONSE_EMPTY"
    recorder = _LocalTransportRecorder()
    monkeypatch.setattr(services, "local_transport_outbound", recorder)
    services.process_outbox(session, "test")
    assert outbox.status == "PENDING" and outbox.last_error == "AUTOMATIC_RESPONSE_EMPTY"
    assert outbox.attempt_count == 0
    assert not recorder.calls
