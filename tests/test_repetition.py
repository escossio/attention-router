from types import SimpleNamespace

from sqlalchemy import func, select
from attention_router.domain.models import now_utc

from attention_router.application import services
from attention_router.application import repetition
from attention_router.config import settings
from attention_router.infrastructure.models import AuditEventRow, OutboxMessageRow


def receive(session, event_id: str, text: str):
    result = services.receive_inbound_event(
        session, "synthetic", event_id, "message", "repeat-actor", "Synthetic", "family_core", None, text,
    )
    session.commit()
    return result


def mark_delivered(session, text: str | None = None):
    outbox = session.scalars(select(OutboxMessageRow).order_by(OutboxMessageRow.created_at.desc())).first()
    if text:
        outbox.payload = {**outbox.payload, "text": text}
    outbox.status = "DONE"
    outbox.completed_at = now_utc()
    session.commit()


def direct_event():
    return SimpleNamespace(
        source="wwebjs",
        event_type="message",
        lineage_classification="ORGANIC",
        payload={
            "channel": "whatsapp",
            "event_type": "message",
            "event_origin": "EXTERNAL_INBOUND",
            "owner_authenticated": False,
            "external_actor_id": "5500000000022@c.us",
            "metadata": {
                "from_me": False,
                "source_account": "default",
                "conversation_state": "READY",
                "conversation_key": "wwebjs:5500000000022@c.us",
                "peer_identifiers": ["5500000000022@c.us"],
            },
        },
    )


def test_fresh_direct_turn_same_objective_is_not_suppressed(session, monkeypatch):
    decision = SimpleNamespace(objective="communication/tone_adaptation", recommended_action="respond")
    interaction = SimpleNamespace(id="interaction-test", contact_id="actor@c.us", inbound_text="Pode explicar de novo?")
    monkeypatch.setattr(
        repetition,
        "check_repetition",
        lambda *args: (_ for _ in ()).throw(AssertionError("fresh direct turn reached repeat guard")),
    )
    result = repetition.suppress_if_repeated(session, decision, interaction, direct_event())
    assert result.suppress is False


def test_non_direct_repeat_still_suppresses(session, monkeypatch):
    decision = SimpleNamespace(objective="communication/tone_adaptation", recommended_action="respond")
    interaction = SimpleNamespace(id="interaction-test", contact_id="actor@c.us", inbound_text="Pode explicar de novo?")
    monkeypatch.setattr(
        repetition,
        "check_repetition",
        lambda *args: repetition.RepetitionDecision(
            True,
            "SEMANTIC_REPEAT_OBJECTIVE_ALREADY_SATISFIED",
            "synthetic",
        ),
    )
    result = repetition.suppress_if_repeated(session, decision, interaction)
    assert result.suppress is True


def test_same_state_suppresses_legacy_outbound(session, monkeypatch):
    monkeypatch.setattr(settings, "agent_decision_pipeline_enabled", False)
    receive(session, "repeat-1", "Pode me chamar de Morgan")
    mark_delivered(session)
    receive(session, "repeat-2", "pode me chamar Morgan.")
    assert session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 1
    assert session.scalar(select(func.count()).select_from(AuditEventRow).where(
        AuditEventRow.event_type == "response_suppressed"
    )) == 1


def test_direct_question_without_delivered_answer_is_allowed(session, monkeypatch):
    monkeypatch.setattr(settings, "agent_decision_pipeline_enabled", False)
    receive(session, "identity-1", "Qual é o seu nome?")
    receive(session, "identity-2", "Como você se chama?")
    assert session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 2
    assert session.scalar(select(func.count()).select_from(AuditEventRow).where(
        AuditEventRow.event_type == "response_suppressed"
    )) == 0


def test_generated_or_suppressed_without_delivery_does_not_satisfy_objective(session, monkeypatch):
    monkeypatch.setattr(settings, "agent_decision_pipeline_enabled", False)
    receive(session, "identity-generated", "Qual é o seu nome?")
    receive(session, "identity-repeat", "Qual é o seu nome?")
    assert session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 2


def test_delivered_semantic_variant_is_suppressed(session, monkeypatch):
    monkeypatch.setattr(settings, "agent_decision_pipeline_enabled", False)
    receive(session, "identity-delivered", "Qual é o seu nome?")
    mark_delivered(session, "Meu nome é Andy, uma assistente virtual.")
    receive(session, "identity-variant", "Como você se chama?")
    assert session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 1
    suppressed = session.scalars(select(AuditEventRow).where(AuditEventRow.event_type == "response_suppressed")).all()
    assert len(suppressed) == 1
    assert suppressed[0].payload["reason"] == "SEMANTIC_REPEAT_OBJECTIVE_ALREADY_SATISFIED"
    assert suppressed[0].payload["previous_useful_response_delivered"] is True


def test_session_boundary_allows_same_objective_again(session, monkeypatch):
    monkeypatch.setattr(settings, "agent_decision_pipeline_enabled", False)
    first = receive(session, "session-1", "Qual é o seu nome?")
    mark_delivered(session)
    second = services.receive_inbound_event(
        session, "synthetic", "session-2", "message", "repeat-actor", "Synthetic", "family_core", "new-session", "Qual é o seu nome?",
    )
    session.commit()
    assert first["id"] != second["id"]
    assert session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 2


def test_new_information_and_new_intent_break_suppression(session, monkeypatch):
    monkeypatch.setattr(settings, "agent_decision_pipeline_enabled", False)
    receive(session, "state-1", "Preciso falar com ele.")
    mark_delivered(session)
    receive(session, "state-2", "Preciso falar com ele.")
    receive(session, "state-3", "É sobre trabalho.")
    receive(session, "state-4", "Você é uma IA?")
    assert session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 3
    assert session.scalar(select(func.count()).select_from(AuditEventRow).where(
        AuditEventRow.event_type == "response_suppressed"
    )) == 1


def test_one_hundred_repeats_do_not_create_unbounded_outbound(session, monkeypatch):
    monkeypatch.setattr(settings, "agent_decision_pipeline_enabled", False)
    variants = [
        "Pode me chamar de Morgan",
        "pode me chamar Morgan",
        "Me chama de Morgan.",
    ]
    for index in range(100):
        receive(session, f"flood-{index}", variants[index % len(variants)])
        if index == 0:
            mark_delivered(session)
    assert session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 1
