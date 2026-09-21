from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from attention_router.application import services
from attention_router.application.personal_context_recommendation_reply import (
    parse_recommendation_reply,
)
from attention_router.application.personal_context_runtime import (
    PERSONAL_CONTEXT_SUGGESTION_OUTBOX_ACTION,
    run_personal_context_runtime_cycle,
)
from attention_router.application.personal_context_suggestion_reply import (
    parse_suggestion_reply,
    resolve_explicit_suggestion_reply,
)
from attention_router.infrastructure.models import (
    AgentExecutionIntentRow,
    ExecutionIntentRow,
    FactRow,
    InboundEventRow,
    MemoryClaimRow,
    OutboxMessageRow,
    ReminderRow,
)
from tests.test_owner_control import _owner_event
from tests.test_personal_context_anomaly_suggestions import _persist_anomaly
from tests.test_personal_context_sequences import (
    ACTOR,
    EXTERNAL,
    _install_owner,
)


class _TransportRecorder:
    def __init__(self):
        self.calls = []

    def dispatch_outbox(self, row):
        self.calls.append(row.id)
        return type(
            "Result",
            (),
            {
                "status": "sent",
                "response": {"message_reference": "pc-v1p-delivered"},
            },
        )()


def _reply(event_id: str, text: str, stamp: datetime):
    return _owner_event(event_id, text).model_copy(
        update={
            "occurred_at": stamp,
            "received_at": stamp,
            "actor_id": EXTERNAL,
        }
    )


def _execution_counts(session) -> tuple[int, int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(ExecutionIntentRow)),
        session.scalar(select(func.count()).select_from(AgentExecutionIntentRow)),
        session.scalar(select(func.count()).select_from(ReminderRow)),
        session.scalar(select(func.count()).select_from(FactRow)),
    )


def _prepare_suggestion(session, stamp: datetime, *, delivery_enabled: bool):
    _install_owner(session)
    _source, _anomaly, detected_at = _persist_anomaly(session, stamp)
    result = run_personal_context_runtime_cycle(
        session,
        now=detected_at,
        delivery_enabled=False,
        suggestion_delivery_enabled=delivery_enabled,
    )
    suggestion = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate == "context.suggestion.proactive",
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    assert suggestion is not None
    return detected_at, result, suggestion


def _delivered_suggestion(session, monkeypatch, stamp: datetime):
    detected_at, result, suggestion = _prepare_suggestion(
        session,
        stamp,
        delivery_enabled=True,
    )
    assert result.suggestions_enqueued == 1
    outbox = session.scalar(
        select(OutboxMessageRow).where(
            OutboxMessageRow.action_type
            == PERSONAL_CONTEXT_SUGGESTION_OUTBOX_ACTION
        )
    )
    assert outbox is not None
    recorder = _TransportRecorder()
    monkeypatch.setattr(
        services,
        "local_transport_outbound",
        recorder,
    )
    with monkeypatch.context() as clock:
        clock.setattr(
            services,
            "now_utc",
            lambda: detected_at + timedelta(minutes=1),
        )
        assert services.process_outbox(session, "v1p-worker") == 1
    session.refresh(outbox)
    assert outbox.status == "DONE"
    assert recorder.calls == [outbox.id]
    return detected_at, suggestion, outbox


def test_suggestion_delivery_is_independent_and_off_when_not_requested(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _detected_at, result, suggestion = _prepare_suggestion(
        session,
        stamp,
        delivery_enabled=False,
    )

    assert result.suggestions_persisted == 1
    assert result.suggestions_enqueued == 0
    assert suggestion.object_json["lifecycle_state"] == "PROPOSED"
    assert session.scalar(
        select(func.count())
        .select_from(OutboxMessageRow)
        .where(
            OutboxMessageRow.action_type
            == PERSONAL_CONTEXT_SUGGESTION_OUTBOX_ACTION
        )
    ) == 0


def test_suggestion_delivery_uses_explicit_non_ambiguous_reply_contract(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _detected_at, result, _suggestion = _prepare_suggestion(
        session,
        stamp,
        delivery_enabled=True,
    )

    assert result.suggestions_enqueued == 1
    outbox = session.scalar(
        select(OutboxMessageRow).where(
            OutboxMessageRow.action_type
            == PERSONAL_CONTEXT_SUGGESTION_OUTBOX_ACTION
        )
    )
    assert outbox is not None
    assert outbox.destination == "local_transport"
    assert outbox.execution_intent_id is None
    assert "quero revisar" in outbox.payload["text"]
    assert "não quero revisar" in outbox.payload["text"]
    assert "não executa nenhuma ação" in outbox.payload["text"]


def test_delivered_quero_revisar_records_interest_without_execution(
    session,
    monkeypatch,
):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _detected_at, original, delivered = _delivered_suggestion(
        session,
        monkeypatch,
        stamp,
    )
    before = _execution_counts(session)
    delivered_at = (
        delivered.completed_at.replace(tzinfo=UTC)
        if delivered.completed_at.tzinfo is None
        else delivered.completed_at.astimezone(UTC)
    )

    result = services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1p-interested",
            "quero revisar",
            delivered_at + timedelta(minutes=1),
        ),
    )

    current = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate == "context.suggestion.proactive",
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    session.refresh(original)
    assert original.status == "SUPERSEDED"
    assert current is not None
    assert current.object_json["lifecycle_state"] == "INTERESTED"
    assert current.object_json["capability_name"] is None
    assert current.object_json["execution_requested"] is False
    assert current.object_json["grants_authority"] is False
    assert current.context["resolution_kind"] == "INTERESTED"
    assert current.context["resolution_inbound_event_id"] == (
        result["inbound_event_id"]
    )
    assert _execution_counts(session) == before

    confirmation = session.scalar(
        select(OutboxMessageRow)
        .where(
            OutboxMessageRow.idempotency_key.like(
                "owner-control:confirmation:%"
            )
        )
        .order_by(OutboxMessageRow.created_at.desc())
    )
    assert confirmation is not None
    assert "não autoriza execução" in confirmation.payload["text"]
    assert "não revela informações adicionais" in confirmation.payload["text"]


def test_delivered_nao_quero_revisar_dismisses_without_execution(
    session,
    monkeypatch,
):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _detected_at, _original, delivered = _delivered_suggestion(
        session,
        monkeypatch,
        stamp,
    )
    before = _execution_counts(session)
    delivered_at = (
        delivered.completed_at.replace(tzinfo=UTC)
        if delivered.completed_at.tzinfo is None
        else delivered.completed_at.astimezone(UTC)
    )

    services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1p-dismiss",
            "não quero revisar",
            delivered_at + timedelta(minutes=1),
        ),
    )

    current = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate == "context.suggestion.proactive",
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    assert current is not None
    assert current.object_json["lifecycle_state"] == "DISMISSED"
    assert current.object_json["capability_name"] is None
    assert _execution_counts(session) == before


def test_reply_before_delivery_does_not_resolve_suggestion(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    detected_at, _result, suggestion = _prepare_suggestion(
        session,
        stamp,
        delivery_enabled=True,
    )
    receipt = services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1p-before-delivery",
            "quero revisar",
            detected_at + timedelta(minutes=1),
        ),
    )
    event = session.get(InboundEventRow, receipt["inbound_event_id"])
    assert event is not None
    resolved = resolve_explicit_suggestion_reply(
        session,
        receipt=event,
        actor_key=ACTOR,
        text="quero revisar",
    )
    session.refresh(suggestion)

    assert resolved is None
    assert suggestion.status == "ACTIVE"
    assert suggestion.object_json["lifecycle_state"] == "PROPOSED"


def test_generic_yes_is_not_a_suggestion_reply():
    assert parse_suggestion_reply("sim") is None
    assert parse_suggestion_reply("não") is None
    assert parse_suggestion_reply("quero revisar").value == "INTERESTED"
    assert parse_suggestion_reply("não quero revisar").value == "DISMISS"
    assert parse_recommendation_reply("quero revisar") is None
    assert parse_recommendation_reply("não quero revisar") is None


def test_stale_suggestion_reply_is_consumed_without_interest_transition(
    session,
    monkeypatch,
):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _detected_at, suggestion, delivered = _delivered_suggestion(
        session,
        monkeypatch,
        stamp,
    )
    anomaly = session.get(
        MemoryClaimRow,
        suggestion.context["source_anomaly_claim_id"],
    )
    assert anomaly is not None
    anomaly.status = "SUPERSEDED"
    session.flush()
    before = _execution_counts(session)
    delivered_at = (
        delivered.completed_at.replace(tzinfo=UTC)
        if delivered.completed_at.tzinfo is None
        else delivered.completed_at.astimezone(UTC)
    )

    services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1p-stale",
            "quero revisar",
            delivered_at + timedelta(minutes=1),
        ),
    )
    session.refresh(suggestion)

    assert suggestion.status == "ACTIVE"
    assert suggestion.object_json["lifecycle_state"] == "PROPOSED"
    assert _execution_counts(session) == before

    confirmation = session.scalar(
        select(OutboxMessageRow)
        .where(
            OutboxMessageRow.idempotency_key.like(
                "owner-control:confirmation:%"
            )
        )
        .order_by(OutboxMessageRow.created_at.desc())
    )
    assert confirmation is not None
    assert "não é mais válida" in confirmation.payload["text"]
    assert "não vou executar nem revelar nada" in confirmation.payload["text"].lower()
