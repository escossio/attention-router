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
    ExecutionIntentRow,
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
                "response": {"message_reference": "pc-v1h-delivered"},
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
    assert services.process_outbox(session, "v1h-worker") == 1
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

    result = services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1h-accept",
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
    assert session.scalar(
        select(func.count()).select_from(ExecutionIntentRow)
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(ReminderRow)
    ) == 0

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
    _delivered_proposal(session, monkeypatch, stamp)

    services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1h-dismiss",
            "não",
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
    assert recommendation.object_json["lifecycle_state"] == "DISMISSED"
    assert session.scalar(
        select(func.count()).select_from(ExecutionIntentRow)
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(ReminderRow)
    ) == 0


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
            "v1h-before-delivery",
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


def test_recommendation_reply_parser_is_closed_and_deterministic():
    assert parse_recommendation_reply("sim").value == "ACCEPT"
    assert parse_recommendation_reply("pode criar").value == "ACCEPT"
    assert parse_recommendation_reply("não").value == "DISMISS"
    assert parse_recommendation_reply("deixa pra lá").value == "DISMISS"
    assert parse_recommendation_reply("talvez amanhã") is None
    assert parse_recommendation_reply("faça qualquer coisa") is None
