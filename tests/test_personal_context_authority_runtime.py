from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from attention_router.application.personal_context_authority_runtime import (
    PersonalContextAuthorityRuntimeResult,
    run_personal_context_authority_cycle,
)
from attention_router.application.platform.authority import create_capability_grant
from attention_router.application.platform.capability_pack import (
    provision_internal_providers,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure import worker
from attention_router.infrastructure.models import (
    AgentExecutionIntentRow,
    ExecutionIntentRow,
    FactRow,
    OutboxMessageRow,
    ReminderRow,
)
from tests.test_personal_context_execution_authority import (
    ACTOR,
    _accepted_recommendation,
    _install_policy,
)


def _side_effect_counts(session) -> tuple[int, int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(ReminderRow)),
        session.scalar(select(func.count()).select_from(OutboxMessageRow)),
        session.scalar(select(func.count()).select_from(AgentExecutionIntentRow)),
        session.scalar(select(func.count()).select_from(FactRow)),
    )


def _grant(session, stamp: datetime):
    return create_capability_grant(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        grantor_type="OPERATOR",
        grantor_id="test",
        grantee_type="ACTOR",
        grantee_id=ACTOR,
        capability_name="reminder.create",
        valid_from=stamp - timedelta(seconds=1),
        valid_until=stamp + timedelta(days=1),
        provenance="test",
    )


def test_authority_runtime_prepares_inert_intent_idempotently(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    binding, _accepted = _accepted_recommendation(session, stamp)
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    _install_policy(session, binding.id, allow_reminder=True)
    _grant(session, stamp)
    before = _side_effect_counts(session)

    first = run_personal_context_authority_cycle(
        session,
        now=stamp + timedelta(minutes=2),
    )
    second = run_personal_context_authority_cycle(
        session,
        now=stamp + timedelta(minutes=3),
    )

    assert first.owners_scanned == 1
    assert first.recommendations_scanned == 1
    assert first.assessments_completed == 1
    assert first.intents_prepared == 1
    assert second.assessments_completed == 1
    assert second.intents_prepared == 1

    intents = session.scalars(select(ExecutionIntentRow)).all()
    assert len(intents) == 1
    assert intents[0].state == "PREPARED"
    assert intents[0].frozen_at is None
    assert intents[0].retired_at is None
    assert _side_effect_counts(session) == before


def test_authority_runtime_retries_denied_acceptance_after_grant_appears(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    binding, _accepted = _accepted_recommendation(session, stamp)
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    _install_policy(session, binding.id, allow_reminder=True)

    denied = run_personal_context_authority_cycle(
        session,
        now=stamp + timedelta(minutes=2),
    )
    assert denied.assessments_completed == 1
    assert denied.denied == 1
    assert denied.intents_prepared == 0
    assert session.scalar(
        select(func.count()).select_from(ExecutionIntentRow)
    ) == 0

    _grant(session, stamp)
    prepared = run_personal_context_authority_cycle(
        session,
        now=stamp + timedelta(minutes=3),
    )
    assert prepared.assessments_completed == 1
    assert prepared.intents_prepared == 1
    assert session.scalar(
        select(func.count()).select_from(ExecutionIntentRow)
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(ReminderRow)
    ) == 0


def test_authority_runtime_fails_closed_when_source_hypothesis_is_invalidated(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    binding, accepted = _accepted_recommendation(session, stamp)
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    _install_policy(session, binding.id, allow_reminder=True)
    _grant(session, stamp)

    source = session.get(
        type(accepted),
        accepted.context["source_claim_id"],
    )
    assert source is not None
    source.status = "SUPERSEDED"
    session.flush()

    result = run_personal_context_authority_cycle(
        session,
        now=stamp + timedelta(minutes=2),
    )

    assert result.assessments_completed == 1
    assert result.source_invalidated == 1
    assert result.intents_prepared == 0
    assert session.scalar(
        select(func.count()).select_from(ExecutionIntentRow)
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(ReminderRow)
    ) == 0


def test_worker_authority_runtime_gate_is_off_by_default(session, monkeypatch):
    monkeypatch.setattr(
        worker.settings,
        "personal_context_authority_runtime_enabled",
        False,
    )

    result, last = worker.process_personal_context_authority_runtime_if_due(
        session,
        now_monotonic=100.0,
        last_run_monotonic=42.0,
    )

    assert result is None
    assert last == 42.0


def test_worker_authority_runtime_gate_runs_independently(session, monkeypatch):
    calls = []

    def fake_cycle(_session, **kwargs):
        calls.append(kwargs)
        return PersonalContextAuthorityRuntimeResult(
            owners_scanned=1,
            recommendations_scanned=1,
            assessments_completed=1,
            intents_prepared=1,
        )

    monkeypatch.setattr(
        worker.settings,
        "personal_context_authority_runtime_enabled",
        True,
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
        "personal_context_runtime_owner_limit",
        17,
    )
    monkeypatch.setattr(
        worker,
        "run_personal_context_authority_cycle",
        fake_cycle,
    )

    result, last = worker.process_personal_context_authority_runtime_if_due(
        session,
        now_monotonic=1000.0,
        last_run_monotonic=None,
    )
    skipped, unchanged = worker.process_personal_context_authority_runtime_if_due(
        session,
        now_monotonic=1100.0,
        last_run_monotonic=last,
    )

    assert result is not None
    assert result.intents_prepared == 1
    assert last == 1000.0
    assert calls == [{"owner_limit": 17}]
    assert skipped is None
    assert unchanged == 1000.0
