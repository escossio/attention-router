from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from attention_router.application import autonomy, execution
from attention_router.application.repetition import RepetitionDecision
from attention_router.config import settings
from attention_router.domain.models import new_id
from attention_router.infrastructure.models import (
    AgentBlueprintRow,
    AgentBlueprintVersionRow,
    AgentDecisionRow,
    AgentExecutionIntentRow,
    AgentResponseReviewRow,
    AutonomyEvaluationRow,
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
    PolicyVersionRow,
)


def _fixture(session, *, blueprint_mode="AUTO_ALLOWED", policy_mode="AUTO_ALLOWED", from_me=False, action="soft_ping"):
    now = datetime.now(timezone.utc)
    blueprint = AgentBlueprintRow(
        id=f"blueprint-{new_id()}", name="Canary", status="published", created_at=now, updated_at=now,
    )
    session.add(blueprint)
    version = AgentBlueprintVersionRow(
        id=f"blueprint-version-{new_id()}", blueprint_id=blueprint.id, version=1,
        spec={"autonomy": {"execution_mode": blueprint_mode}, "missing_information": [], "escalation": []},
        checksum=new_id(), created_at=now, created_by="test", change_reason="test", is_immutable=True,
    )
    blueprint.current_version_id = version.id
    policy = PolicyVersionRow(
        id=f"policy-version-{new_id()}", policy_id="desconhecido", version=99,
        config={
            "allowed_actions": [action], "execution_mode": policy_mode,
            "execution_scope": {"audiences": ["canary"]},
        }, checksum=new_id(), created_at=now, created_by="test", is_immutable=True,
    )
    event = InboundEventRow(
        id=f"event-{new_id()}", source="wwebjs", external_event_id=f"external-{new_id()}", event_type="message",
        payload={"external_actor_id": "5500000000022@c.us", "channel": "whatsapp", "from_me": from_me,
            "event_type":"message", "event_origin":"EXTERNAL_INBOUND", "owner_authenticated":False,
            "metadata":{"from_me":from_me,"source_account":"default","conversation_state":"READY",
                "conversation_key":"wwebjs:5500000000022@c.us"}},
        lineage_classification="ORGANIC",
        payload_hash=new_id(), received_at=now, status="processed", correlation_id=new_id(),
    )
    interaction = InteractionRow(
        id=f"interaction-{new_id()}", event_type="message", contact_id="actor@c.us", contact_name="Canary",
        relationship_category="canary", inbound_text="hello", state="active", policy_id="desconhecido",
        policy_version_id=policy.id, correlation_id=event.correlation_id, created_at=now, updated_at=now,
    )
    event.interaction_id = interaction.id
    decision = AgentDecisionRow(
        id=f"decision-{new_id()}", event_id=event.id, interaction_id=interaction.id,
        agent_blueprint_id=blueprint.id, agent_blueprint_version=1, policy_version_id=policy.id,
        audience="canary",
        decision_pipeline_version="v1", decision_type="RESPOND", recommended_action=action,
        proposed_response="Canary response", escalation_required=False, confidence=0.9, missing_information=[],
        execution_allowed=False, external_delivery_allowed=False, reasoning_summary="test", status="DRY_RUN",
        created_at=now,
    )
    session.add_all([version, policy, event, interaction, decision])
    session.flush()
    return decision


def test_default_deny_and_observe_path(session, monkeypatch):
    decision = _fixture(session, blueprint_mode="OBSERVE", policy_mode="AUTO_ALLOWED")
    monkeypatch.setattr(settings, "autonomous_execution_enabled", True)
    monkeypatch.setattr(settings, "external_delivery_enabled", True)
    monkeypatch.setattr(settings, "autonomous_execution_activated_at", datetime.now(timezone.utc).isoformat())
    evaluation = autonomy.evaluate_and_route(session, decision)
    assert evaluation.effective_mode == "OBSERVE"
    assert evaluation.automatic_execution_allowed is False
    assert session.scalar(select(AgentExecutionIntentRow).where(AgentExecutionIntentRow.agent_decision_id == decision.id)) is None


def test_requires_approval_creates_pending_review(session, monkeypatch):
    decision = _fixture(session, blueprint_mode="AUTO_ALLOWED", policy_mode="REQUIRES_APPROVAL")
    calls = []
    monkeypatch.setattr(
        autonomy,
        "suppress_if_repeated",
        lambda *args: calls.append(True) or RepetitionDecision(False, None, "synthetic"),
    )
    monkeypatch.setattr(settings, "autonomous_execution_enabled", True)
    monkeypatch.setattr(settings, "external_delivery_enabled", True)
    monkeypatch.setattr(settings, "autonomous_execution_activated_at", datetime.now(timezone.utc).isoformat())
    evaluation = autonomy.evaluate_and_route(session, decision)
    assert evaluation.effective_mode == "REQUIRES_APPROVAL"
    review = session.scalar(select(AgentResponseReviewRow).where(AgentResponseReviewRow.agent_decision_id == decision.id))
    assert review is not None and review.status == "PENDING"
    assert session.scalar(select(AgentExecutionIntentRow).where(AgentExecutionIntentRow.agent_decision_id == decision.id)) is None
    assert calls == [True]


def test_auto_allowed_creates_one_policy_authorized_intent_and_outbox(session, monkeypatch):
    decision = _fixture(session)
    monkeypatch.setattr(settings, "autonomous_execution_enabled", True)
    monkeypatch.setattr(settings, "external_delivery_enabled", True)
    monkeypatch.setattr(settings, "autonomous_execution_activated_at", (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    evaluation = autonomy.evaluate_and_route(session, decision)
    assert evaluation.automatic_execution_allowed is True
    assert session.scalar(select(AgentResponseReviewRow).where(AgentResponseReviewRow.agent_decision_id == decision.id)) is None
    intent = session.scalar(select(AgentExecutionIntentRow).where(AgentExecutionIntentRow.agent_decision_id == decision.id))
    assert intent is not None
    assert intent.authorization_source == "POLICY_AUTONOMY"
    assert execution.enqueue_ready_intents(session, transport_ready=True) == 1
    assert execution.enqueue_ready_intents(session, transport_ready=True) == 0
    assert len(session.scalars(select(OutboxMessageRow).where(OutboxMessageRow.execution_intent_id == intent.id)).all()) == 1


def test_auto_path_blocks_self_message_and_stale_event(session, monkeypatch):
    decision = _fixture(session, from_me=True)
    monkeypatch.setattr(settings, "autonomous_execution_enabled", True)
    monkeypatch.setattr(settings, "external_delivery_enabled", True)
    monkeypatch.setattr(settings, "autonomous_execution_activated_at", (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    evaluation = autonomy.evaluate_and_route(session, decision)
    assert evaluation.reason_code == "SELF_MESSAGE"
    assert evaluation.automatic_execution_allowed is False
    assert session.scalar(select(AgentExecutionIntentRow).where(AgentExecutionIntentRow.agent_decision_id == decision.id)) is None


def test_request_information_is_allowed_by_matching_policy_scope(session, monkeypatch):
    decision = _fixture(session, action="request_information")
    monkeypatch.setattr(settings, "autonomous_execution_enabled", True)
    monkeypatch.setattr(settings, "external_delivery_enabled", True)
    monkeypatch.setattr(settings, "autonomous_execution_activated_at", (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    evaluation = autonomy.evaluate_and_route(session, decision)
    assert evaluation.action_allowed is True
    assert evaluation.automatic_execution_allowed is True


def test_non_allowed_action_is_blocked_by_policy_scope(session, monkeypatch):
    decision = _fixture(session, action="soft_ping")
    policy = session.get(PolicyVersionRow, decision.policy_version_id)
    policy.config = {**policy.config, "allowed_actions": ["request_information"]}
    monkeypatch.setattr(settings, "autonomous_execution_enabled", True)
    monkeypatch.setattr(settings, "external_delivery_enabled", True)
    monkeypatch.setattr(settings, "autonomous_execution_activated_at", (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    evaluation = autonomy.evaluate_and_route(session, decision)
    assert evaluation.reason_code == "ACTION_NOT_ALLOWED"
    assert evaluation.automatic_execution_allowed is False


def test_nested_from_me_is_blocked(session, monkeypatch):
    decision = _fixture(session)
    event = session.get(InboundEventRow, decision.event_id)
    event.payload = {**event.payload, "from_me": False, "metadata": {"fromMe": True}}
    monkeypatch.setattr(settings, "autonomous_execution_enabled", True)
    monkeypatch.setattr(settings, "external_delivery_enabled", True)
    monkeypatch.setattr(settings, "autonomous_execution_activated_at", (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    evaluation = autonomy.evaluate_and_route(session, decision)
    assert evaluation.reason_code == "SELF_MESSAGE"
    assert evaluation.automatic_execution_allowed is False


def test_autonomy_evaluation_is_idempotent(session):
    decision = _fixture(session, blueprint_mode="OBSERVE", policy_mode="OBSERVE")
    first = autonomy.evaluate_and_route(session, decision)
    second = autonomy.evaluate_and_route(session, decision)
    assert first.id == second.id
    assert len(session.scalars(select(AutonomyEvaluationRow).where(AutonomyEvaluationRow.agent_decision_id == decision.id)).all()) == 1
