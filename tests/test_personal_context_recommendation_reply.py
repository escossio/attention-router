from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from attention_router.application import services
from attention_router.application.personal_context_recommendation_reply import (
    parse_recommendation_reply,
)
from attention_router.application.personal_context_runtime import (
    PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION,
    run_personal_context_runtime_cycle,
)
from attention_router.infrastructure.models import (
    AgentExecutionIntentRow,
    ExecutionIntentRow,
    FactRow,
    MemoryClaimRow,
    OutboxMessageRow,
    ReminderRow,
)
from tests.test_owner_control import _owner_event
from tests.test_personal_context_runtime import _setup_runtime


class _TransportRecorder:
    def dispatch_outbox(self, row):
        return type(
            "Result",
            (),
            {
                "status": "sent",
                "response": {"message_reference": "pc-v1i-delivered"},
            },
        )()


def _delivered_proposal(session, monkeypatch, stamp: datetime):
    binding = _setup_runtime(session, stamp)
    run_personal_context_runtime_cycle(
        session,
        now=stamp,
        delivery_enabled=True,
    )
    recommendation_outbox = session.scalar(
        select(OutboxMessageRow).where(
            OutboxMessageRow.action_type
            == PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION
        )
    )
    assert recommendation_outbox is not None

    monkeypatch.setattr(
        services,
        "local_transport_outbound",
        _TransportRecorder(),
    )
    with monkeypatch.context() as clock:
        clock.setattr(
            services,
            "now_utc",
            lambda: stamp + timedelta(minutes=1),
        )
        assert services.process_outbox(session, "v1i-worker") == 1
    session.refresh(recommendation_outbox)
    assert recommendation_outbox.status == "DONE"
    return binding, recommendation_outbox


def _reply(event_id: str, text: str, stamp: datetime):
    return _owner_event(event_id, text).model_copy(
        update={
            "occurred_at": stamp,
            "received_at": stamp,
            "actor_id": "owner-v1g@c.us",
        }
    )


def _side_effect_counts(session) -> tuple[int, int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(ExecutionIntentRow)),
        session.scalar(select(func.count()).select_from(AgentExecutionIntentRow)),
        session.scalar(select(func.count()).select_from(ReminderRow)),
        session.scalar(select(func.count()).select_from(FactRow)),
    )


def test_explicit_yes_accepts_one_delivered_recommendation_without_execution(
    session,
    monkeypatch,
):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _binding, delivered = _delivered_proposal(
        session,
        monkeypatch,
        stamp,
    )
    before = _side_effect_counts(session)

    result = services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1i-accept",
            "sim",
            (
                delivered.completed_at.replace(tzinfo=UTC)
                if delivered.completed_at.tzinfo is None
                else delivered.completed_at.astimezone(UTC)
            )
            + timedelta(minutes=1),
        ),
    )

    recommendation = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate
            == "context.recommendation.proactive",
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    assert recommendation is not None
    assert recommendation.object_json["lifecycle_state"] == "ACCEPTED"
    assert recommendation.object_json["execution_requested"] is False
    assert recommendation.object_json["grants_authority"] is False
    assert recommendation.context["resolution_kind"] == "ACCEPT"
    assert recommendation.context["resolution_inbound_event_id"] == (
        result["inbound_event_id"]
    )
    assert delivered.payload["recommendation_id"] == (
        recommendation.context["recommendation_id"]
    )
    assert _side_effect_counts(session) == before

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
    assert "revalidar as permissões" in confirmation.payload["text"]


def test_explicit_no_dismisses_delivered_recommendation(session, monkeypatch):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _binding, delivered = _delivered_proposal(
        session,
        monkeypatch,
        stamp,
    )
    before = _side_effect_counts(session)

    delivered_at = (
        delivered.completed_at.replace(tzinfo=UTC)
        if delivered.completed_at.tzinfo is None
        else delivered.completed_at.astimezone(UTC)
    )
    services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1i-dismiss",
            "não",
            delivered_at + timedelta(minutes=1),
        ),
    )

    recommendation = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate
            == "context.recommendation.proactive",
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    assert recommendation is not None
    assert recommendation.object_json["lifecycle_state"] == "DISMISSED"
    assert _side_effect_counts(session) == before


def test_reply_before_delivery_does_not_accept_recommendation(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _setup_runtime(session, stamp)
    run_personal_context_runtime_cycle(
        session,
        now=stamp,
        delivery_enabled=True,
    )

    services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1i-before-delivery",
            "sim",
            stamp + timedelta(minutes=1),
        ),
    )

    recommendation = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate
            == "context.recommendation.proactive",
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    assert recommendation is not None
    assert recommendation.object_json["lifecycle_state"] == "PROPOSED"


def test_stale_yes_is_consumed_safely_after_source_invalidation(
    session,
    monkeypatch,
):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _binding, delivered = _delivered_proposal(
        session,
        monkeypatch,
        stamp,
    )
    proposal = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate == "context.recommendation.proactive",
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    assert proposal is not None
    source = session.get(
        MemoryClaimRow,
        proposal.context["source_claim_id"],
    )
    assert source is not None
    source.status = "SUPERSEDED"
    session.flush()
    before = _side_effect_counts(session)

    delivered_at = (
        delivered.completed_at.replace(tzinfo=UTC)
        if delivered.completed_at.tzinfo is None
        else delivered.completed_at.astimezone(UTC)
    )
    services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1i-stale-source",
            "sim",
            delivered_at + timedelta(minutes=1),
        ),
    )

    session.refresh(proposal)
    assert proposal.status == "ACTIVE"
    assert proposal.object_json["lifecycle_state"] == "PROPOSED"
    assert _side_effect_counts(session) == before

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
    assert "Não vou executar nada" in confirmation.payload["text"]


def test_recommendation_reply_parser_is_closed_and_deterministic():
    assert parse_recommendation_reply("sim").value == "ACCEPT"
    assert parse_recommendation_reply("pode criar").value == "ACCEPT"
    assert parse_recommendation_reply("não").value == "DISMISS"
    assert parse_recommendation_reply("deixa pra lá").value == "DISMISS"
    assert parse_recommendation_reply("talvez amanhã") is None
    assert parse_recommendation_reply("faça qualquer coisa") is None
