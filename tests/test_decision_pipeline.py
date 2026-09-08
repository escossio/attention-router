from datetime import datetime, timezone

from sqlalchemy import select

from attention_router.adapters.inbound import NormalizedInboundEvent
from attention_router.application import services
from attention_router.application.agents.output import AndyAgentOutput
from attention_router.application.agents.service import AndyAgentResult
from attention_router.application.decision_pipeline import process_agent_decision
from attention_router.infrastructure.worker import process_agent_decisions
from attention_router.config import settings
from attention_router.domain.models import new_id
from attention_router.domain.policies import resolve_policy
from attention_router.infrastructure.models import (
    AgentBlueprintRow,
    AgentBlueprintVersionRow,
    AgentDecisionRow,
    AuditEventRow,
    EntityStateRow,
    OutboxMessageRow,
    QueueRow,
)
from attention_router.infrastructure.repository import create_policy, list_policies, upsert_actor_binding


def test_decision_pipeline_is_worker_driven_and_idempotent(session):
    now = datetime.now(timezone.utc)
    blueprint = AgentBlueprintRow(
        id="blueprint-test",
        name="Blueprint de teste",
        status="published",
        current_version_id="blueprint-version-test",
        created_at=now,
        updated_at=now,
    )
    session.add(blueprint)
    session.add(
        AgentBlueprintVersionRow(
            id="blueprint-version-test",
            blueprint_id=blueprint.id,
            version=1,
            spec={
                "domain": "personal_attention",
                "autonomy": {"level": "observe"},
                "missing_information": [],
                "escalation": [],
            },
            checksum="checksum-test",
            created_at=now,
            created_by="test",
            change_reason="test",
            is_immutable=True,
        )
    )
    upsert_actor_binding(
        session,
        source="test",
        external_actor_id="actor-test",
        actor_key="actor-key-test",
        actor_category="known",
        display_name="Synthetic actor",
        metadata={"agent_blueprint_id": blueprint.id, "audience": "configured-audience"},
    )
    session.flush()
    event = NormalizedInboundEvent(
        source="test",
        external_event_id=f"event-{new_id()}",
        event_type="message",
        actor_id="actor-test",
        actor_display_name="Synthetic actor",
        actor_category="known",
        channel="test",
        content="Mensagem sintetica para decisao.",
    )
    result = services.receive_normalized_inbound_event(session, event)
    session.commit()

    queue = session.get(QueueRow, f"decision:{result['inbound_event_id']}")
    assert queue is not None
    assert session.scalars(
        select(OutboxMessageRow).where(OutboxMessageRow.interaction_id == result["id"])
    ).all() == []
    assert process_agent_decisions(session, "test-worker") == 1
    session.commit()
    decision = session.scalar(select(AgentDecisionRow).where(AgentDecisionRow.event_id == result["inbound_event_id"]))
    session.commit()
    assert process_agent_decisions(session, "test-worker-retry") == 0
    duplicate = process_agent_decision(session, result["inbound_event_id"])

    assert decision.id == duplicate.id
    assert decision.decision_type == "RESPOND"
    assert decision.audience == "configured-audience"
    assert decision.execution_allowed is False
    assert decision.external_delivery_allowed is False
    assert session.scalar(select(AgentDecisionRow).where(AgentDecisionRow.event_id == result["inbound_event_id"])) is not None
    audit_types = set(
        session.scalars(
            select(AuditEventRow.event_type).where(AuditEventRow.interaction_id == result["id"])
        ).all()
    )
    assert {"decision.started", "actor.resolved", "agent.resolved", "audience.resolved", "policy.selected", "decision.persisted"} <= audit_types


def test_decision_pipeline_passes_represented_owner_presence_to_andy(session, monkeypatch):
    now = datetime.now(timezone.utc)
    blueprint = AgentBlueprintRow(
        id="blueprint-represented-context",
        name="Represented context test",
        status="published",
        current_version_id="blueprint-represented-context-version",
        created_at=now,
        updated_at=now,
    )
    session.add(blueprint)
    session.add(
        AgentBlueprintVersionRow(
            id=blueprint.current_version_id,
            blueprint_id=blueprint.id,
            version=1,
            spec={"domain": "personal_attention", "autonomy": {"level": "observe"}, "missing_information": []},
            checksum="represented-context-checksum",
            created_at=now,
            created_by="test",
            change_reason="test",
            is_immutable=True,
        )
    )
    upsert_actor_binding(
        session,
        source="test",
        external_actor_id="owner-external",
        actor_key="owner-a",
        actor_category="owner",
        display_name="Tenant owner",
        metadata={"owner": True},
    )
    upsert_actor_binding(
        session,
        source="test",
        external_actor_id="morgan-external",
        actor_key="morgan-a",
        actor_category="known",
        display_name="Sr. Morgan",
        metadata={"agent_blueprint_id": blueprint.id, "audience": "configured-audience"},
    )
    session.add(
        EntityStateRow(
            id="owner-a-presence",
            tenant_id="00000000-0000-4000-8000-000000000001",
            subject_type="ACTOR",
            subject_id="owner-a",
            state_namespace="presence",
            state_key="effective",
            state_value={"status": "sleeping"},
            source="test",
            effective_at=now,
            expires_at=None,
            version=1,
            updated_at=now,
        )
    )
    session.flush()
    monkeypatch.setattr(settings, "andy_agent_enabled", True)
    monkeypatch.setattr("attention_router.application.decision_pipeline._agent_enabled_for", lambda *args, **kwargs: True)
    captured = {}

    def fake_run_andy(context):
        captured["context"] = context
        return AndyAgentResult(
            output=AndyAgentOutput(
                response_text="Andy: posso ajudar.",
                intent="GENERAL",
                objective="general",
                reason_code="TEST",
                confidence="high",
                conversation_state="answer",
            ),
            duration_ms=1,
            turn_count=1,
            tool_call_count=0,
        )

    monkeypatch.setattr("attention_router.application.decision_pipeline.run_andy", fake_run_andy)
    event = NormalizedInboundEvent(
        source="test",
        external_event_id=f"represented-context-{new_id()}",
        event_type="message",
        actor_id="morgan-external",
        actor_display_name="Sr. Morgan",
        actor_category="known",
        channel="test",
        content="oi",
    )
    received = services.receive_normalized_inbound_event(session, event)
    session.commit()
    decision = process_agent_decision(session, received["inbound_event_id"])

    context = captured["context"]
    from attention_router.observability.tracing import identifier_hash
    assert context.interaction_actor == {"type": "ACTOR", "id": identifier_hash("morgan-a")}
    assert context.represented_subject == {"entity_type": "ACTOR", "entity_id": "owner-a"}
    assert context.current_operational_state == []  # No disclosure authority in this fixture.
    built = session.scalar(
        select(AuditEventRow).where(
            AuditEventRow.interaction_id == received["id"],
            AuditEventRow.event_type == "agent_context_built",
        )
    )
    assert built is not None
    assert built.payload["presence_effective"] is False
    assert decision.proposed_response == "Andy: posso ajudar."


def test_decision_pipeline_falls_back_without_agent_or_policy(session):
    event = NormalizedInboundEvent(
        source="test",
        external_event_id=f"event-{new_id()}",
        event_type="message",
        actor_id="unbound",
        actor_display_name="Unknown",
        actor_category="unknown",
        channel="test",
        content="Mensagem sem configuracao.",
    )
    result = services.receive_normalized_inbound_event(session, event)
    session.commit()
    decision = process_agent_decision(session, result["inbound_event_id"])
    assert decision.decision_type == "INSUFFICIENT_CONTEXT"
    assert decision.execution_allowed is False
    assert decision.external_delivery_allowed is False


def test_binding_scoped_policy_beats_generic_fallback(session):
    binding = upsert_actor_binding(
        session,
        source="test",
        external_actor_id="actor-canary",
        actor_key="actor-canary",
        actor_category="family_core",
        display_name="Synthetic canary",
        metadata={"audience": "autonomy_canary"},
    )
    create_policy(
        session,
        "autonomy_canary_test",
        {
            "identifier": "autonomy_canary_test",
            "name": "Scoped canary",
            "match_criteria": {"binding_id": binding.id, "audience": "autonomy_canary"},
            "priority": 1000,
            "specificity": 1000,
            "tone": "neutral",
            "initial_wait_seconds": 1,
            "allowed_disclosures": [],
            "allowed_actions": ["request_information"],
            "escalation_steps": ["request_information"],
            "ack_timeout_seconds": 10,
            "repetition_limit": 1,
            "cancellation_conditions": ["human_reply"],
            "completion_conditions": ["safe_completion"],
        },
    )
    resolution = resolve_policy(
        list_policies(session),
        "actor-canary",
        "family_core",
        None,
        audience="autonomy_canary",
        binding_id=binding.id,
    )
    assert resolution.winner.identifier == "autonomy_canary_test"
    assert any(item["policy_id"] == "desconhecido" for item in resolution.matched)


def test_live_entrypoint_uses_scoped_behavior_profile_in_dry_run(session, monkeypatch):
    now = datetime.now(timezone.utc)
    blueprint = AgentBlueprintRow(
        id="blueprint-live-behavior-test",
        name="Live behavior test blueprint",
        status="published",
        current_version_id="blueprint-live-behavior-version",
        created_at=now,
        updated_at=now,
    )
    session.add(blueprint)
    session.add(
        AgentBlueprintVersionRow(
            id="blueprint-live-behavior-version",
            blueprint_id=blueprint.id,
            version=1,
            spec={"domain": "personal_attention", "autonomy": {"level": "observe"}, "missing_information": ["context"]},
            checksum="live-behavior-checksum",
            created_at=now,
            created_by="test",
            change_reason="test",
            is_immutable=True,
        )
    )
    binding = upsert_actor_binding(
        session,
        source="test",
        external_actor_id="actor-live-behavior",
        actor_key="actor-live-behavior",
        actor_category="unknown",
        display_name="Synthetic live behavior actor",
        metadata={"agent_blueprint_id": blueprint.id, "audience": "unknown"},
    )
    session.flush()
    monkeypatch.setattr(settings, "andy_behavior_enabled", True)
    monkeypatch.setattr(settings, "andy_behavior_canary_binding_id", binding.id)
    event = NormalizedInboundEvent(
        source="test",
        external_event_id=f"event-{new_id()}",
        event_type="message",
        actor_id="actor-live-behavior",
        actor_display_name="Synthetic live behavior actor",
        actor_category="unknown",
        channel="test",
        content="Oi, preciso falar com o Alex.",
    )
    received = services.receive_normalized_inbound_event(session, event)
    session.commit()
    decision = process_agent_decision(session, received["inbound_event_id"])

    assert decision.proposed_response
    assert "Andy" in decision.proposed_response
    assert "Pode me passar mais algumas informações para eu ajudar?" not in decision.proposed_response
    assert decision.response_spoken_text == decision.proposed_response
    assert decision.response_message_family == "REQUEST_CONTEXT"
    assert decision.response_introduction_included is True


def test_behavior_unavailable_fails_closed_without_legacy(session, monkeypatch):
    monkeypatch.setattr(settings, "andy_behavior_enabled", False)
    event = NormalizedInboundEvent(
        source="test",
        external_event_id=f"event-no-behavior-{new_id()}",
        event_type="message",
        actor_id="actor-no-behavior",
        actor_display_name="Synthetic actor",
        actor_category="unknown",
        channel="test",
        content="Mensagem sem Behavior seguro.",
    )
    received = services.receive_normalized_inbound_event(session, event)
    session.commit()

    decision = process_agent_decision(session, received["inbound_event_id"])

    assert decision.proposed_response is None
    assert decision.response_spoken_text is None
    assert session.scalar(select(AgentDecisionRow).where(AgentDecisionRow.id == decision.id)).proposed_response is None
    assert not session.scalar(select(AgentDecisionRow).where(AgentDecisionRow.id == decision.id)).proposed_response
    assert session.scalar(
        select(AuditEventRow).where(
            AuditEventRow.interaction_id == received["id"],
            AuditEventRow.event_type == "behavior.unavailable",
        )
    ).payload["reason"] == "BEHAVIOR_UNAVAILABLE_NO_SAFE_RESPONSE"


def test_behavior_profile_failure_fails_closed_without_legacy(session, monkeypatch):
    binding = upsert_actor_binding(
        session,
        source="test",
        external_actor_id="actor-profile-failure",
        actor_key="actor-profile-failure",
        actor_category="unknown",
        metadata={"audience": "unknown"},
    )
    session.flush()
    monkeypatch.setattr(settings, "andy_behavior_enabled", True)
    monkeypatch.setattr(settings, "andy_behavior_canary_binding_id", binding.id)
    monkeypatch.setattr(settings, "andy_behavior_profile_path", "/missing/behavior-profile.json")
    event = NormalizedInboundEvent(
        source="test",
        external_event_id=f"event-profile-failure-{new_id()}",
        event_type="message",
        actor_id="actor-profile-failure",
        actor_display_name="Synthetic actor",
        actor_category="unknown",
        channel="test",
        content="Mensagem sem perfil carregável.",
    )
    received = services.receive_normalized_inbound_event(session, event)
    session.commit()

    decision = process_agent_decision(session, received["inbound_event_id"])

    assert decision.proposed_response is None
    assert decision.response_message_family is None
