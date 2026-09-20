from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from attention_router.application.personal_context_materialization_runtime import (
    PersonalContextMaterializationRuntimeResult,
    run_personal_context_materialization_cycle,
)
from attention_router.application.platform.authority import revoke_capability_grant
from attention_router.application.platform.capability_pack import (
    process_due_scheduled_events,
)
from attention_router.infrastructure import worker
from attention_router.infrastructure.models import (
    AgentExecutionIntentRow,
    ExecutionIntentRow,
    FactRow,
    OutboxMessageRow,
    ReminderRow,
)
from tests.test_personal_context_execution_materialization import _prepared


def _non_reminder_side_effect_counts(session) -> tuple[int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(OutboxMessageRow)),
        session.scalar(select(func.count()).select_from(AgentExecutionIntentRow)),
        session.scalar(select(func.count()).select_from(FactRow)),
    )


def test_materialization_runtime_creates_one_scheduled_reminder(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _accepted, _grant, _assessment, intent = _prepared(session, stamp)
    before = _non_reminder_side_effect_counts(session)

    result = run_personal_context_materialization_cycle(
        session,
        now=stamp + timedelta(minutes=3),
    )
    session.refresh(intent)

    assert result.intents_scanned == 1
    assert result.materialized == 1
    assert result.failed == 0
    assert intent.state == "MATERIALIZED"

    reminders = session.scalars(select(ReminderRow)).all()
    assert len(reminders) == 1
    assert reminders[0].status == "SCHEDULED"
    assert reminders[0].idempotency_key == intent.idempotency_key
    assert _non_reminder_side_effect_counts(session) == before


def test_materialization_runtime_does_not_duplicate_materialized_intent(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _accepted, _grant, _assessment, intent = _prepared(session, stamp)

    first = run_personal_context_materialization_cycle(
        session,
        now=stamp + timedelta(minutes=3),
    )
    second = run_personal_context_materialization_cycle(
        session,
        now=stamp + timedelta(minutes=4),
    )

    assert first.materialized == 1
    assert second.intents_scanned == 0
    assert second.materialized == 0
    assert session.scalar(
        select(func.count()).select_from(ReminderRow)
    ) == 1
    session.refresh(intent)
    assert intent.state == "MATERIALIZED"


def test_materialization_runtime_retires_intent_when_grant_was_revoked(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _accepted, grant, _assessment, intent = _prepared(session, stamp)
    revoke_capability_grant(
        session,
        tenant_id=intent.scope["tenant_id"],
        grant_id=grant.id,
    )

    result = run_personal_context_materialization_cycle(
        session,
        now=stamp + timedelta(minutes=3),
    )
    session.refresh(intent)

    assert result.intents_scanned == 1
    assert result.authority_blocked == 1
    assert result.materialized == 0
    assert intent.state == "RETIRED"
    assert session.scalar(
        select(func.count()).select_from(ReminderRow)
    ) == 0


def test_materialization_runtime_retires_intent_after_source_invalidation(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    accepted, _grant, _assessment, intent = _prepared(session, stamp)
    source = session.get(
        type(accepted),
        accepted.context["source_claim_id"],
    )
    assert source is not None
    source.status = "SUPERSEDED"
    session.flush()

    result = run_personal_context_materialization_cycle(
        session,
        now=stamp + timedelta(minutes=3),
    )
    session.refresh(intent)

    assert result.intents_scanned == 1
    assert result.authority_blocked == 1
    assert result.materialized == 0
    assert intent.state == "RETIRED"
    assert session.scalar(
        select(func.count()).select_from(ReminderRow)
    ) == 0


def test_materialized_reminder_fires_internal_event_without_external_delivery(
    session,
):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _accepted, _grant, _assessment, _intent = _prepared(session, stamp)
    before = _non_reminder_side_effect_counts(session)

    result = run_personal_context_materialization_cycle(
        session,
        now=stamp + timedelta(minutes=3),
    )
    assert result.materialized == 1

    reminder = session.scalar(select(ReminderRow))
    assert reminder is not None
    assert reminder.status == "SCHEDULED"

    fired = process_due_scheduled_events(
        session,
        now=stamp + timedelta(days=1, minutes=1),
    )
    session.refresh(reminder)

    assert fired == 1
    assert reminder.status == "FIRED"
    assert reminder.fired_at is not None
    assert _non_reminder_side_effect_counts(session) == before


def test_worker_materialization_gate_is_off_by_default(session, monkeypatch):
    monkeypatch.setattr(
        worker.settings,
        "personal_context_materialization_runtime_enabled",
        False,
    )

    result, last = worker.process_personal_context_materialization_runtime_if_due(
        session,
        now_monotonic=100.0,
        last_run_monotonic=42.0,
    )

    assert result is None
    assert last == 42.0


def test_worker_materialization_gate_runs_independently(session, monkeypatch):
    calls = []

    def fake_cycle(_session, **kwargs):
        calls.append(kwargs)
        return PersonalContextMaterializationRuntimeResult(
            intents_scanned=1,
            materialized=1,
        )

    monkeypatch.setattr(
        worker.settings,
        "personal_context_materialization_runtime_enabled",
        True,
    )
    monkeypatch.setattr(
        worker.settings,
        "personal_context_authority_runtime_enabled",
        False,
    )
    monkeypatch.setattr(
        worker.settings,
        "personal_context_runtime_enabled",
        False,
    )
    monkeypatch.setattr(
        worker.settings,
        "personal_context_runtime_interval_seconds",
        300,
    )
    monkeypatch.setattr(
        worker.settings,
        "personal_context_materialization_intent_limit",
        17,
    )
    monkeypatch.setattr(
        worker,
        "run_personal_context_materialization_cycle",
        fake_cycle,
    )

    result, last = worker.process_personal_context_materialization_runtime_if_due(
        session,
        now_monotonic=1000.0,
        last_run_monotonic=None,
    )
    skipped, unchanged = worker.process_personal_context_materialization_runtime_if_due(
        session,
        now_monotonic=1100.0,
        last_run_monotonic=last,
    )

    assert result is not None
    assert result.materialized == 1
    assert last == 1000.0
    assert calls == [{"intent_limit": 17}]
    assert skipped is None
    assert unchanged == 1000.0
