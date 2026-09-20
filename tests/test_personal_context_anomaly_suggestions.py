from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from attention_router.application.personal_context_anomalies import (
    detect_missing_step_anomalies,
)
from attention_router.application.personal_context_anomaly_hypotheses import (
    persist_missing_step_anomaly,
    reconcile_missing_step_anomalies,
)
from attention_router.application.personal_context_anomaly_suggestions import (
    build_anomaly_suggestions,
)
from attention_router.application.personal_context_runtime import (
    run_personal_context_runtime_cycle,
)
from attention_router.application.personal_context_suggestion_lifecycle import (
    persist_anomaly_suggestion,
    reconcile_anomaly_suggestions,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import (
    ExecutionIntentRow,
    FactRow,
    MemoryClaimRow,
    OutboxMessageRow,
    ReminderRow,
)
from tests.test_personal_context_anomalies import (
    _persist_sequence,
    _post_learning_first_event,
)
from tests.test_personal_context_sequences import ACTOR, _install_owner


def _persist_anomaly(session, stamp: datetime):
    _hypothesis, source = _persist_sequence(session, stamp)
    first = _post_learning_first_event(session, stamp=stamp)
    detected_at = first.occurred_at + timedelta(minutes=47)
    anomaly = detect_missing_step_anomalies(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=detected_at,
    )[0]
    claim, changed = persist_missing_step_anomaly(
        session,
        anomaly=anomaly,
        now=detected_at,
    )
    assert changed is True
    return source, claim, detected_at


def _effect_counts(session) -> tuple[int, int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(ExecutionIntentRow)),
        session.scalar(select(func.count()).select_from(ReminderRow)),
        session.scalar(select(func.count()).select_from(OutboxMessageRow)),
        session.scalar(select(func.count()).select_from(FactRow)),
    )


def test_active_missing_step_anomaly_builds_review_suggestion(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    source, anomaly, detected_at = _persist_anomaly(session, stamp)

    items = build_anomaly_suggestions(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=detected_at,
    )

    assert len(items) == 1
    item = items[0]
    assert item.source_anomaly_claim_id == anomaly.id
    assert item.source_sequence_claim_id == source.id
    assert item.suggestion_type == "REVIEW_MISSING_ROUTINE_STEP"
    assert item.status == "PROPOSED"
    assert item.requires_user_confirmation is True
    assert item.execution_requested is False
    assert item.grants_authority is False
    assert "não apareceu" in item.explanation
    assert item.valid_until <= detected_at + timedelta(hours=24)


def test_anomaly_suggestion_persists_idempotently_without_effects(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _source, _anomaly, detected_at = _persist_anomaly(session, stamp)
    suggestion = build_anomaly_suggestions(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=detected_at,
    )[0]
    before = _effect_counts(session)

    first, changed = persist_anomaly_suggestion(
        session,
        suggestion=suggestion,
        now=detected_at,
    )
    replay, replay_changed = persist_anomaly_suggestion(
        session,
        suggestion=suggestion,
        now=detected_at,
    )

    assert changed is True
    assert replay_changed is False
    assert replay.id == first.id
    assert first.predicate == "context.suggestion.proactive"
    assert first.source_quality == "DERIVED_SUGGESTION"
    assert first.object_json["suggestion_type"] == (
        "REVIEW_MISSING_ROUTINE_STEP"
    )
    assert first.object_json["lifecycle_state"] == "PROPOSED"
    assert first.object_json["capability_name"] is None
    assert first.object_json["execution_requested"] is False
    assert first.object_json["grants_authority"] is False
    assert _effect_counts(session) == before


def test_late_expected_step_supersedes_anomaly_and_derived_suggestion(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _source, anomaly, detected_at = _persist_anomaly(session, stamp)
    suggestion = build_anomaly_suggestions(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=detected_at,
    )[0]
    suggestion_claim, _ = persist_anomaly_suggestion(
        session,
        suggestion=suggestion,
        now=detected_at,
    )

    first_event_id = anomaly.context["first_event_id"]
    first = session.get(
        __import__(
            "attention_router.infrastructure.models",
            fromlist=["TimelineEventRow"],
        ).TimelineEventRow,
        first_event_id,
    )
    assert first is not None
    from tests.test_personal_context_anomalies import _expected_second_event

    _expected_second_event(session, first_at=first.occurred_at)
    reconcile_missing_step_anomalies(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=detected_at + timedelta(minutes=1),
    )
    changed = reconcile_anomaly_suggestions(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=detected_at + timedelta(minutes=1),
    )
    session.refresh(anomaly)
    session.refresh(suggestion_claim)

    assert anomaly.status == "SUPERSEDED"
    assert changed == 1
    assert suggestion_claim.status == "SUPERSEDED"
    assert suggestion_claim.context["terminal_reason"] == (
        "SOURCE_ANOMALY_INACTIVE"
    )


def test_runtime_persists_review_suggestion_without_delivery_or_execution(session):
    _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _source, _anomaly, detected_at = _persist_anomaly(session, stamp)
    before = _effect_counts(session)

    result = run_personal_context_runtime_cycle(
        session,
        now=detected_at,
        delivery_enabled=True,
    )

    assert result.suggestions_built == 1
    assert result.suggestions_persisted == 1
    suggestion = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate == "context.suggestion.proactive",
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    assert suggestion is not None
    assert suggestion.object_json["lifecycle_state"] == "PROPOSED"
    assert suggestion.object_json["capability_name"] is None
    assert _effect_counts(session) == before
