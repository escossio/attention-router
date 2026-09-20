from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from attention_router.application import services
from attention_router.application.personal_context_review import (
    is_context_review_request,
)
from attention_router.application.personal_context_suggestion_reply import (
    parse_suggestion_reply,
)
from attention_router.application.personal_context_recommendation_reply import (
    parse_recommendation_reply,
)
from attention_router.infrastructure.models import (
    AgentExecutionIntentRow,
    ExecutionIntentRow,
    FactRow,
    MemoryClaimRow,
    OutboxMessageRow,
    ReminderRow,
)
from tests.test_personal_context_suggestion_delivery import (
    _delivered_suggestion,
    _prepare_suggestion,
    _reply,
)


def _execution_counts(session) -> tuple[int, int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(ExecutionIntentRow)),
        session.scalar(select(func.count()).select_from(AgentExecutionIntentRow)),
        session.scalar(select(func.count()).select_from(ReminderRow)),
        session.scalar(select(func.count()).select_from(FactRow)),
    )


def _make_interested(session, monkeypatch, stamp: datetime):
    _detected_at, original, delivered = _delivered_suggestion(
        session,
        monkeypatch,
        stamp,
    )
    delivered_at = (
        delivered.completed_at.replace(tzinfo=UTC)
        if delivered.completed_at.tzinfo is None
        else delivered.completed_at.astimezone(UTC)
    )
    services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1q-interest",
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
    return current, delivered_at + timedelta(minutes=1)


def test_review_request_parser_is_closed_and_separate():
    assert is_context_review_request("mostrar revisão") is True
    assert is_context_review_request("mostrar essa revisao") is True
    assert is_context_review_request("ver revisão") is True
    assert is_context_review_request("quero revisar") is False
    assert is_context_review_request("sim") is False
    assert parse_suggestion_reply("mostrar revisão") is None
    assert parse_recommendation_reply("mostrar revisão") is None


def test_interested_owner_receives_structural_only_review(
    session,
    monkeypatch,
):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    interested, interest_at = _make_interested(
        session,
        monkeypatch,
        stamp,
    )
    before = _execution_counts(session)

    result = services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1q-review",
            "mostrar revisão",
            interest_at + timedelta(minutes=1),
        ),
    )

    reviewed = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate == "context.suggestion.proactive",
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    session.refresh(interested)

    assert interested.status == "SUPERSEDED"
    assert reviewed is not None
    assert reviewed.object_json["lifecycle_state"] == "REVIEWED"
    assert reviewed.object_json["capability_name"] is None
    assert reviewed.object_json["execution_requested"] is False
    assert reviewed.object_json["grants_authority"] is False
    assert reviewed.object_json["review_disclosure_class"] == "STRUCTURAL_ONLY"
    assert reviewed.context["review_disclosure_class"] == "STRUCTURAL_ONLY"
    assert reviewed.context["review_inbound_event_id"] == (
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
    text = confirmation.payload["text"]
    assert "Revisão mínima:" in text
    assert "3 ocorrências" in text
    assert "100%" in text
    assert "31 minutos" in text
    assert "Nenhuma ação foi executada." in text

    # Structural-only means no raw semantic signature, source/provider label,
    # internal ID or owner transport identity leaks into the rendered review.
    forbidden = (
        "home-departure",
        "gym-arrival",
        "device-location",
        "android-location",
        "calendar",
        "sms",
        "@c.us",
        interested.context["source_anomaly_claim_id"],
        interested.context["source_sequence_claim_id"],
    )
    assert all(item not in text for item in forbidden)


def test_review_without_interested_suggestion_reveals_nothing(
    session,
    monkeypatch,
):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _detected_at, _result, suggestion = _prepare_suggestion(
        session,
        stamp,
        delivery_enabled=True,
    )
    before = _execution_counts(session)

    services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1q-no-interest",
            "mostrar revisão",
            stamp + timedelta(hours=2),
        ),
    )
    session.refresh(suggestion)

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
    assert "Não há uma revisão ativa" in confirmation.payload["text"]
    assert "home-departure" not in confirmation.payload["text"]
    assert "gym-arrival" not in confirmation.payload["text"]


def test_source_invalidation_after_interest_blocks_review_disclosure(
    session,
    monkeypatch,
):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    interested, interest_at = _make_interested(
        session,
        monkeypatch,
        stamp,
    )
    anomaly = session.get(
        MemoryClaimRow,
        interested.context["source_anomaly_claim_id"],
    )
    assert anomaly is not None
    anomaly.status = "SUPERSEDED"
    session.flush()
    before = _execution_counts(session)

    services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1q-stale",
            "mostrar revisão",
            interest_at + timedelta(minutes=1),
        ),
    )
    session.refresh(interested)

    assert interested.status == "ACTIVE"
    assert interested.object_json["lifecycle_state"] == "INTERESTED"
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
    assert "não está mais disponível" in confirmation.payload["text"]
    assert "Nenhum contexto adicional foi revelado." in confirmation.payload["text"]
    assert "home-departure" not in confirmation.payload["text"]
    assert "gym-arrival" not in confirmation.payload["text"]


def test_secret_source_after_interest_fails_closed_without_disclosure(
    session,
    monkeypatch,
):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    interested, interest_at = _make_interested(
        session,
        monkeypatch,
        stamp,
    )
    sequence = session.get(
        MemoryClaimRow,
        interested.context["source_sequence_claim_id"],
    )
    assert sequence is not None
    sequence.sensitivity_class = "SECRET"
    session.flush()

    services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1q-secret",
            "mostrar revisão",
            interest_at + timedelta(minutes=1),
        ),
    )
    session.refresh(interested)

    assert interested.object_json["lifecycle_state"] == "INTERESTED"
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
    assert "Nenhum contexto adicional foi revelado." in confirmation.payload["text"]
