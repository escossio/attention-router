from __future__ import annotations

from sqlalchemy import func, select

from attention_router.application import services
from attention_router.application.user_idiolect_projection import (
    project_resolved_pending_intent_language_fact,
)
from attention_router.infrastructure.models import FactRow
from tests.test_owner_control import _install_owner_and_grace
from tests.test_owner_control_clarification import _event, _start_pending


def test_resolved_clarification_projects_one_confirmed_language_fact(
    session,
    monkeypatch,
):
    _install_owner_and_grace(session)
    _, pending = _start_pending(session, monkeypatch)

    services.receive_normalized_inbound_event(
        session,
        _event("idiolect-resolve", "2", offset_seconds=10),
    )
    session.refresh(pending)

    fact = session.scalar(
        select(FactRow).where(FactRow.source_ref == pending.id)
    )
    assert fact is not None
    assert fact.fact_class == "USER_CONFIRMED_LANGUAGE"
    assert fact.source_type == "INTENT_CLARIFICATION"
    assert fact.subject_type == "ACTOR"
    assert fact.subject_id == pending.represented_owner_actor_key
    assert fact.predicate == "idiolect.pragmatic_mapping"
    assert fact.confidence == 1.0
    assert fact.valid_from == pending.resolved_at
    assert fact.valid_until is not None
    assert fact.valid_until > fact.valid_from
    assert fact.value_json["expression"] == "retorne em 30 segundos"
    assert fact.value_json["semantic_intent_key"] == "ONE_SHOT_REPLY_DELAY"
    assert fact.value_json["parameters"] == {"seconds": 30}
    assert fact.value_json["direction"] == "USER_TO_ANDY_LANGUAGE"
    assert fact.value_json["evidence_class"] == "EXPLICITLY_CONFIRMED"
    assert fact.value_json["reuse_policy"] == "INTERPRET_ONLY"
    assert fact.value_json["generalization_scope"] == "CONVERSATION"


def test_projection_is_idempotent_for_same_pending_intent(session, monkeypatch):
    _install_owner_and_grace(session)
    _, pending = _start_pending(session, monkeypatch)
    services.receive_normalized_inbound_event(
        session,
        _event("idiolect-idempotent", "2", offset_seconds=10),
    )
    session.refresh(pending)

    first = project_resolved_pending_intent_language_fact(
        session,
        pending_intent_id=pending.id,
    )
    second = project_resolved_pending_intent_language_fact(
        session,
        pending_intent_id=pending.id,
    )

    assert first is not None
    assert second is not None
    assert first.id == second.id
    count = session.scalar(
        select(func.count())
        .select_from(FactRow)
        .where(FactRow.source_ref == pending.id)
    )
    assert count == 1


def test_canceled_clarification_does_not_project_confirmed_fact(
    session,
    monkeypatch,
):
    _install_owner_and_grace(session)
    _, pending = _start_pending(session, monkeypatch)

    services.receive_normalized_inbound_event(
        session,
        _event("idiolect-cancel", "nenhuma", offset_seconds=10),
    )
    session.refresh(pending)

    assert pending.state == "CANCELED"
    assert project_resolved_pending_intent_language_fact(
        session,
        pending_intent_id=pending.id,
    ) is None
    assert session.scalar(
        select(func.count())
        .select_from(FactRow)
        .where(FactRow.source_ref == pending.id)
    ) == 0
