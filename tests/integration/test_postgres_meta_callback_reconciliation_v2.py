from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from attention_router.infrastructure.models import (
    AgentExecutionIntentRow,
    AuditEventRow,
    BoundedRunAuthorizationRow,
    EffectBudgetRow,
    EffectConsumptionRow,
    ExecutionLeaseRow,
    HumanExecutionAuthorizationRow,
    MetaCallbackEvidenceRow,
    MetaCallbackAuditMarkerRow,
    MetaCallbackInboxRow,
    MetaDeliveryReconciliationRow,
    OutboxMessageRow,
    ScenarioRunRow,
)
from attention_router.platform.meta_callback_reconciliation import (
    META_DELIVERY_REASON,
    META_FAILED_REASON,
    META_INVALID_REASON,
    META_TIMEOUT_REASON,
    MetaAdmissionUnavailable,
    admit_meta_callback_evidence,
    recover_quarantined_meta_reconciliation,
    reconcile_due_meta_callback_outcomes,
    reconcile_meta_callback_outcome,
    reconcile_pending_meta_callback_inbox,
)
from attention_router.platform.production_authority import ProductionAuthorityDenied
from attention_router.platform.production_bridge import materialize_production_agent_intent
from attention_router.platform.production_controlled_execution import (
    mark_production_meta_api_accepted,
    prepare_production_controlled_execution,
    reserve_production_conversation_reply,
)
from tests.integration.test_postgres_10_5c_6i1a_r2_bridge_adversarial import (
    _decision,
    _parent,
)


pytestmark = pytest.mark.postgres


@dataclass(frozen=True, slots=True)
class AcceptedAttempt:
    provider_message_id: str
    reconciliation_id: str
    outbox_id: str
    run_id: str
    budget_id: str
    lease_id: str
    consumption_id: str
    bounded_id: str
    authorization_id: str
    agent_id: str
    accepted_at: datetime
    deadline_at: datetime
    closure_after: datetime


@dataclass(frozen=True, slots=True)
class ReservedAttempt:
    provider_message_id: str
    outbox_id: str
    run_id: str
    authorization_id: str
    agent_id: str
    reserved_at: datetime


def _accepted_attempt(Session, *, suffix: str | None = None) -> AcceptedAttempt:
    suffix = suffix or uuid4().hex
    accepted_at = datetime.now(UTC)
    provider_message_id = f"wamid.v2.{suffix}"
    with Session() as session:
        parent = _parent(session, suffix)
        decision = _decision(session, suffix)
        authorization = session.scalar(
            select(HumanExecutionAuthorizationRow).where(
                HumanExecutionAuthorizationRow.execution_intent_id == parent.id
            )
        )
        authorization.decision_at = accepted_at
        agent = materialize_production_agent_intent(
            session,
            execution_intent_id=parent.id,
            expected_fingerprint=parent.scope_fingerprint,
            agent_decision_id=decision,
            effective_response_snapshot="ok",
        )
        prepared = prepare_production_controlled_execution(
            session,
            agent_execution_intent_id=agent.id,
            authorization_id=authorization.id,
            run_id=f"v2-run-{suffix}",
            root_correlation_id=f"v2-correlation-{suffix}",
            lease_ttl_seconds=120,
            now=accepted_at,
        )
        reserved = reserve_production_conversation_reply(
            session,
            agent_execution_intent_id=agent.id,
            authorization_id=authorization.id,
            dispatcher_id="v2-test-dispatcher",
            now=accepted_at,
        )
        mark_production_meta_api_accepted(
            session,
            outbox_message_id=reserved.outbox_message_id,
            dispatcher_id="v2-test-dispatcher",
            provider_message_id=provider_message_id,
            now=accepted_at,
        )
        reconciliation = session.scalar(
            select(MetaDeliveryReconciliationRow).where(
                MetaDeliveryReconciliationRow.provider_message_id
                == provider_message_id
            )
        )
        session.commit()
        return AcceptedAttempt(
            provider_message_id=provider_message_id,
            reconciliation_id=reconciliation.id,
            outbox_id=reserved.outbox_message_id,
            run_id=prepared.scenario_run_id,
            budget_id=prepared.effect_budget_id,
            lease_id=prepared.execution_lease_id,
            consumption_id=reserved.consumption_id,
            bounded_id=prepared.bounded_authorization_id,
            authorization_id=authorization.id,
            agent_id=agent.id,
            accepted_at=accepted_at,
            deadline_at=reconciliation.deadline_at,
            closure_after=reconciliation.closure_after,
        )


def _reserved_attempt(Session, *, suffix: str | None = None) -> ReservedAttempt:
    suffix = suffix or uuid4().hex
    reserved_at = datetime.now(UTC)
    with Session() as session:
        parent = _parent(session, suffix)
        decision = _decision(session, suffix)
        authorization = session.scalar(
            select(HumanExecutionAuthorizationRow).where(
                HumanExecutionAuthorizationRow.execution_intent_id == parent.id
            )
        )
        authorization.decision_at = reserved_at
        agent = materialize_production_agent_intent(
            session,
            execution_intent_id=parent.id,
            expected_fingerprint=parent.scope_fingerprint,
            agent_decision_id=decision,
            effective_response_snapshot="ok",
        )
        prepared = prepare_production_controlled_execution(
            session,
            agent_execution_intent_id=agent.id,
            authorization_id=authorization.id,
            run_id=f"v2-early-run-{suffix}",
            root_correlation_id=f"v2-early-correlation-{suffix}",
            lease_ttl_seconds=120,
            now=reserved_at,
        )
        reserved = reserve_production_conversation_reply(
            session,
            agent_execution_intent_id=agent.id,
            authorization_id=authorization.id,
            dispatcher_id="v2-test-dispatcher",
            now=reserved_at,
        )
        session.commit()
        return ReservedAttempt(
            provider_message_id=f"wamid.v2.early.{suffix}",
            outbox_id=reserved.outbox_message_id,
            run_id=prepared.scenario_run_id,
            authorization_id=authorization.id,
            agent_id=agent.id,
            reserved_at=reserved_at,
        )


def _status(attempt: AcceptedAttempt, status: str, timestamp: int | str):
    return {
        "id": attempt.provider_message_id,
        "status": status,
        "timestamp": timestamp,
        "recipient_id": None,
        "errors": [],
    }


def _terminal_event_count(session, attempt: AcceptedAttempt) -> int:
    return session.scalar(
        select(func.count()).select_from(AuditEventRow).where(
            AuditEventRow.event_type.in_(
                [
                    "production_conversation_reply_delivered",
                    "production_conversation_reply_failed",
                ]
            ),
            AuditEventRow.payload["outbox_id"].as_string() == attempt.outbox_id,
        )
    )


def _row_snapshot(session, model, identifier):
    row = session.get(model, identifier)
    assert row is not None
    return tuple((column.name, getattr(row, column.name)) for column in model.__table__.columns)


def _terminal_graph_snapshot(session, attempt: AcceptedAttempt):
    rows = (
        (ScenarioRunRow, attempt.run_id),
        (OutboxMessageRow, attempt.outbox_id),
        (AgentExecutionIntentRow, attempt.agent_id),
        (HumanExecutionAuthorizationRow, attempt.authorization_id),
        (BoundedRunAuthorizationRow, attempt.bounded_id),
        (EffectBudgetRow, attempt.budget_id),
        (ExecutionLeaseRow, attempt.lease_id),
        (EffectConsumptionRow, attempt.consumption_id),
        (MetaDeliveryReconciliationRow, attempt.reconciliation_id),
    )
    terminal_events = session.execute(
        select(
            AuditEventRow.id,
            AuditEventRow.event_type,
            AuditEventRow.payload,
            AuditEventRow.created_at,
        )
        .where(
            AuditEventRow.event_type.in_(
                [
                    "production_conversation_reply_delivered",
                    "production_conversation_reply_failed",
                ]
            ),
            AuditEventRow.payload["outbox_id"].as_string() == attempt.outbox_id,
        )
        .order_by(AuditEventRow.id)
    ).all()
    evidences = session.execute(
        select(
            MetaCallbackEvidenceRow.id,
            MetaCallbackEvidenceRow.deduplication_key,
            MetaCallbackEvidenceRow.provider_status,
            MetaCallbackEvidenceRow.provider_timestamp_raw,
            MetaCallbackEvidenceRow.provider_timestamp,
            MetaCallbackEvidenceRow.received_at,
            MetaCallbackEvidenceRow.valid,
            MetaCallbackEvidenceRow.admissible,
            MetaCallbackEvidenceRow.errors_present,
            MetaCallbackEvidenceRow.error_fingerprint,
            MetaCallbackEvidenceRow.created_at,
        )
        .where(MetaCallbackEvidenceRow.reconciliation_id == attempt.reconciliation_id)
        .order_by(MetaCallbackEvidenceRow.id)
    ).all()
    return {
        "rows": tuple(_row_snapshot(session, model, identifier) for model, identifier in rows),
        "terminal_events": tuple(terminal_events),
        "evidences": tuple(evidences),
        "agent_outbox_count": session.scalar(
            select(func.count()).select_from(OutboxMessageRow).where(
                OutboxMessageRow.execution_intent_id == attempt.agent_id
            )
        ),
        "agent_consumption_count": session.scalar(
            select(func.count()).select_from(EffectConsumptionRow).where(
                EffectConsumptionRow.execution_intent_id == attempt.agent_id
            )
        ),
    }


def test_api_accepted_consumes_once_and_creates_one_normalized_root(Session):
    attempt = _accepted_attempt(Session)
    assert (attempt.deadline_at - attempt.accepted_at).total_seconds() == 604800
    assert (attempt.closure_after - attempt.deadline_at).total_seconds() == 30
    with Session() as session:
        outbox = session.get(OutboxMessageRow, attempt.outbox_id)
        budget = session.get(EffectBudgetRow, attempt.budget_id)
        lease = session.get(ExecutionLeaseRow, attempt.lease_id)
        consumption = session.get(EffectConsumptionRow, attempt.consumption_id)
        bounded = session.get(BoundedRunAuthorizationRow, attempt.bounded_id)
        authorization = session.get(
            HumanExecutionAuthorizationRow, attempt.authorization_id
        )
        assert (outbox.status, outbox.attempt_count) == ("AWAITING_DELIVERY", 1)
        assert (budget.status, budget.reserved_count, budget.consumed_count) == (
            "CONSUMED",
            0,
            1,
        )
        assert lease.status == consumption.state == bounded.status == "CONSUMED"
        assert authorization.state == "CONSUMED"
        assert session.scalar(
            select(func.count()).select_from(MetaDeliveryReconciliationRow).where(
                MetaDeliveryReconciliationRow.outbox_message_id == attempt.outbox_id
            )
        ) == 1
        acceptance_events = session.scalar(
            select(func.count()).select_from(AuditEventRow).where(
                AuditEventRow.event_type == "production_meta_api_accepted",
                AuditEventRow.payload["outbox_id"].as_string() == attempt.outbox_id,
            )
        )
        assert acceptance_events == 1
        mark_production_meta_api_accepted(
            session,
            outbox_message_id=attempt.outbox_id,
            dispatcher_id="v2-test-dispatcher",
            provider_message_id=attempt.provider_message_id,
            now=attempt.accepted_at + timedelta(seconds=1),
        )
        session.commit()
    with Session() as observer:
        assert observer.scalar(
            select(func.count()).select_from(AuditEventRow).where(
                AuditEventRow.event_type == "production_meta_api_accepted",
                AuditEventRow.payload["outbox_id"].as_string() == attempt.outbox_id,
            )
        ) == 1
    reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.closure_after,
    )


def test_admission_timeout_must_fit_the_durable_grace_on_existing_row(
    monkeypatch, Session
):
    monkeypatch.setattr(
        "attention_router.platform.production_controlled_execution."
        "settings.meta_admission_grace_seconds",
        5,
    )
    attempt = _accepted_attempt(Session)
    with pytest.raises(
        MetaAdmissionUnavailable,
        match="ADMISSION_TIMEOUT_EXCEEDS_STORED_GRACE",
    ):
        admit_meta_callback_evidence(
            Session,
            status_event=_status(attempt, "delivered", 1_788_220_800),
            received_at=attempt.deadline_at,
            max_transaction_seconds=10,
            admission_grace_seconds=30,
        )
    with Session() as session:
        assert session.scalar(
            select(func.count()).select_from(MetaCallbackEvidenceRow).where(
                MetaCallbackEvidenceRow.reconciliation_id == attempt.reconciliation_id
            )
        ) == 0
    reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.closure_after,
    )


def test_predeadline_admission_processed_after_deadline_passes_once(Session):
    attempt = _accepted_attempt(Session)
    admission = admit_meta_callback_evidence(
        Session,
        status_event=_status(attempt, "delivered", 1_788_220_800),
        received_at=attempt.deadline_at - timedelta(microseconds=1),
    )
    assert admission.evidence_persisted is True
    result = reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.deadline_at + timedelta(seconds=1),
    )
    assert (result.decision, result.reason_code, result.terminalized) == (
        "PASSED",
        META_DELIVERY_REASON,
        True,
    )
    with Session() as session:
        assert session.get(ScenarioRunRow, attempt.run_id).status == "PASSED"
        assert session.get(OutboxMessageRow, attempt.outbox_id).status == "DONE"
        assert _terminal_event_count(session, attempt) == 1
        assert session.get(OutboxMessageRow, attempt.outbox_id).attempt_count == 1
        assert session.scalar(
            select(func.count()).select_from(OutboxMessageRow).where(
                OutboxMessageRow.execution_intent_id == attempt.agent_id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(EffectConsumptionRow).where(
                EffectConsumptionRow.execution_intent_id == attempt.agent_id
            )
        ) == 1


def test_negative_closure_waits_for_grace_then_uses_latest_provider_time(Session):
    attempt = _accepted_attempt(Session)
    admit_meta_callback_evidence(
        Session,
        status_event=_status(attempt, "sent", 1_788_220_900),
        received_at=attempt.deadline_at,
    )
    admit_meta_callback_evidence(
        Session,
        status_event=_status(attempt, "failed", 1_788_220_800),
        received_at=attempt.deadline_at,
    )
    before = reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.closure_after - timedelta(microseconds=1),
    )
    closed = reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.closure_after,
    )
    assert before.decision == "PENDING"
    assert (closed.decision, closed.reason_code) == ("FAILED", META_TIMEOUT_REASON)


def test_latest_failed_closes_with_provider_failure_reason(Session):
    attempt = _accepted_attempt(Session)
    admit_meta_callback_evidence(
        Session,
        status_event=_status(attempt, "sent", 1_788_220_800),
        received_at=attempt.deadline_at,
    )
    admit_meta_callback_evidence(
        Session,
        status_event=_status(attempt, "failed", 1_788_220_900),
        received_at=attempt.deadline_at,
    )
    closed = reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.closure_after,
    )
    assert (closed.decision, closed.reason_code) == ("FAILED", META_FAILED_REASON)


def test_invalid_evidence_is_normalized_but_cannot_participate(Session):
    attempt = _accepted_attempt(Session)
    event = _status(attempt, "delivered", True)
    admission = admit_meta_callback_evidence(
        Session,
        status_event=event,
        received_at=attempt.deadline_at,
    )
    assert admission.evidence_persisted is True
    with Session() as session:
        evidence = session.scalar(
            select(MetaCallbackEvidenceRow).where(
                MetaCallbackEvidenceRow.reconciliation_id == attempt.reconciliation_id
            )
        )
        assert evidence.valid is False
        assert evidence.provider_timestamp is None
        assert evidence.provider_timestamp_raw == "bool:true"
    closed = reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.closure_after,
    )
    assert (closed.decision, closed.reason_code) == ("FAILED", META_INVALID_REASON)


def test_callback_for_non_meta_production_reply_scope_is_audit_only(Session):
    attempt = _accepted_attempt(Session)
    with Session() as session:
        outbox = session.get(OutboxMessageRow, attempt.outbox_id)
        outbox.destination = "wwebjs"
        session.commit()
    admission = admit_meta_callback_evidence(
        Session,
        status_event=_status(attempt, "delivered", 1_788_220_800),
        received_at=attempt.deadline_at,
    )
    assert admission.scope_accepted is False
    assert admission.evidence_persisted is True
    with Session() as session:
        assert session.scalar(
            select(func.count()).select_from(MetaCallbackEvidenceRow).where(
                MetaCallbackEvidenceRow.reconciliation_id == attempt.reconciliation_id
            )
        ) == 0
        inbox = session.get(MetaCallbackInboxRow, admission.inbox_id)
        assert inbox is not None
        assert inbox.state == "QUARANTINED"
        assert session.get(ScenarioRunRow, attempt.run_id).status == "RUNNING"
        outbox = session.get(OutboxMessageRow, attempt.outbox_id)
        outbox.destination = "meta_whatsapp_cloud"
        session.commit()
    reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.closure_after,
    )


def test_failed_callbacks_with_distinct_errors_are_distinct_durable_evidence(Session):
    attempt = _accepted_attempt(Session)
    first = _status(attempt, "failed", 1_788_220_800)
    second = _status(attempt, "failed", 1_788_220_800)
    first["errors"] = [{"code": 1, "message": "first"}]
    second["errors"] = [{"code": 2, "message": "second"}]
    admissions = [
        admit_meta_callback_evidence(
            Session,
            status_event=event,
            received_at=attempt.deadline_at,
        )
        for event in (first, second)
    ]
    assert all(item.evidence_persisted for item in admissions)
    with Session() as session:
        rows = session.scalars(
            select(MetaCallbackEvidenceRow).where(
                MetaCallbackEvidenceRow.reconciliation_id == attempt.reconciliation_id
            )
        ).all()
        assert len(rows) == 2
        assert len({row.error_fingerprint for row in rows}) == 2
    reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.closure_after,
    )


def test_late_positive_is_persisted_but_cannot_reopen_failed_terminal(Session):
    attempt = _accepted_attempt(Session)
    closed = reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.closure_after,
    )
    assert (closed.decision, closed.reason_code) == ("FAILED", META_TIMEOUT_REASON)
    with Session() as session:
        before = _terminal_graph_snapshot(session, attempt)
    admission = admit_meta_callback_evidence(
        Session,
        status_event=_status(attempt, "read", 1_788_220_999),
        received_at=attempt.deadline_at + timedelta(microseconds=1),
    )
    replay = reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.closure_after + timedelta(seconds=1),
    )
    assert admission.evidence_persisted is True
    assert admission.admissible is False
    assert replay.state_changed is False
    with Session() as session:
        after = _terminal_graph_snapshot(session, attempt)
        assert before["rows"] == after["rows"]
        assert before["terminal_events"] == after["terminal_events"]
        assert before["agent_outbox_count"] == after["agent_outbox_count"]
        assert before["agent_consumption_count"] == after["agent_consumption_count"]
        assert len(after["evidences"]) == len(before["evidences"]) + 1
        assert _terminal_event_count(session, attempt) == 1


def test_late_failed_is_auditable_but_cannot_reopen_passed_terminal(Session):
    attempt = _accepted_attempt(Session)
    admit_meta_callback_evidence(
        Session,
        status_event=_status(attempt, "delivered", 1_788_220_800),
        received_at=attempt.deadline_at,
    )
    passed = reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.deadline_at + timedelta(seconds=1),
    )
    assert passed.decision == "PASSED"
    with Session() as session:
        before = _terminal_graph_snapshot(session, attempt)
    failed = _status(attempt, "failed", 1_788_220_999)
    failed["errors"] = [{"code": 1, "message": "late conflict"}]
    admission = admit_meta_callback_evidence(
        Session,
        status_event=failed,
        received_at=attempt.deadline_at + timedelta(seconds=1),
    )
    replay = reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.closure_after + timedelta(seconds=1),
    )
    assert admission.evidence_persisted is True
    assert admission.admissible is False
    assert replay.state_changed is False
    with Session() as session:
        after = _terminal_graph_snapshot(session, attempt)
        assert before["rows"] == after["rows"]
        assert before["terminal_events"] == after["terminal_events"]
        assert before["agent_outbox_count"] == after["agent_outbox_count"]
        assert before["agent_consumption_count"] == after["agent_consumption_count"]
        assert len(after["evidences"]) == len(before["evidences"]) + 1
        assert _terminal_event_count(session, attempt) == 1


def test_admission_does_not_wait_for_reconciliation_or_outbox_graph_lock(Session):
    attempt = _accepted_attempt(Session)
    with Session() as locker:
        locker.execute(
            select(MetaDeliveryReconciliationRow)
            .where(MetaDeliveryReconciliationRow.id == attempt.reconciliation_id)
            .with_for_update(key_share=True)
        ).scalar_one()
        locker.execute(
            select(OutboxMessageRow)
            .where(OutboxMessageRow.id == attempt.outbox_id)
            .with_for_update()
        ).scalar_one()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                admit_meta_callback_evidence,
                Session,
                status_event=_status(attempt, "delivered", 1_788_220_800),
                received_at=attempt.deadline_at - timedelta(microseconds=1),
            )
            admission = future.result(timeout=5)
        assert admission.evidence_persisted is True
        assert locker.in_transaction()
        locker.rollback()
    result = reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.deadline_at + timedelta(seconds=1),
    )
    assert result.decision == "PASSED"


@pytest.mark.parametrize(
    ("left_status", "left_timestamp", "right_status", "right_timestamp", "expected"),
    [
        ("delivered", 1_788_220_800, "failed", 1_788_220_900, "PASSED"),
        ("read", 1_788_220_800, "failed", 1_788_220_900, "PASSED"),
        ("sent", 1_788_220_800, "failed", 1_788_220_900, "FAILED"),
    ],
)
def test_concurrent_positive_failed_and_sent_failed_are_deterministic(
    Session,
    left_status,
    left_timestamp,
    right_status,
    right_timestamp,
    expected,
):
    attempt = _accepted_attempt(Session)
    barrier = Barrier(2)

    def admit(status, provider_timestamp):
        barrier.wait()
        return admit_meta_callback_evidence(
            Session,
            status_event=_status(attempt, status, provider_timestamp),
            received_at=attempt.deadline_at - timedelta(seconds=1),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(admit, left_status, left_timestamp)
        right = pool.submit(admit, right_status, right_timestamp)
        admissions = [left.result(timeout=10), right.result(timeout=10)]
    assert sum(item.evidence_persisted for item in admissions) == 2
    result = reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.closure_after,
    )
    assert result.decision == expected
    with Session() as session:
        assert _terminal_event_count(session, attempt) == 1
        assert session.get(OutboxMessageRow, attempt.outbox_id).attempt_count == 1
        assert session.scalar(
            select(func.count()).select_from(EffectConsumptionRow).where(
                EffectConsumptionRow.execution_intent_id == attempt.agent_id
            )
        ) == 1
        marker_count = session.scalar(
            select(func.count()).select_from(MetaCallbackAuditMarkerRow).where(
                MetaCallbackAuditMarkerRow.reconciliation_id
                == attempt.reconciliation_id
            )
        )
        assert marker_count == int(expected == "PASSED")


def test_concurrent_identical_callbacks_deduplicate_durably(Session):
    attempt = _accepted_attempt(Session)
    barrier = Barrier(2)

    def admit_duplicate():
        barrier.wait()
        return admit_meta_callback_evidence(
            Session,
            status_event=_status(attempt, "delivered", 1_788_220_800),
            received_at=attempt.deadline_at - timedelta(seconds=1),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(admit_duplicate)
        second = pool.submit(admit_duplicate)
        admissions = [first.result(timeout=10), second.result(timeout=10)]
    assert sum(item.evidence_persisted for item in admissions) == 1
    assert sum(item.duplicate for item in admissions) == 1
    with Session() as session:
        assert session.scalar(
            select(func.count()).select_from(MetaCallbackEvidenceRow).where(
                MetaCallbackEvidenceRow.reconciliation_id == attempt.reconciliation_id
            )
        ) == 1
    reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.deadline_at + timedelta(seconds=1),
    )


def test_concurrent_ingress_and_scanner_have_no_deadlock_or_aborted_session(
    monkeypatch, Session
):
    first = _accepted_attempt(Session)
    second = _accepted_attempt(Session)
    for attempt in (first, second):
        admit_meta_callback_evidence(
            Session,
            status_event=_status(attempt, "delivered", 1_788_220_800),
            received_at=attempt.deadline_at - timedelta(seconds=1),
        )
    ingress_has_first_graph = Event()
    scanner_has_second_graph = Event()
    module = __import__(
        "attention_router.platform.meta_callback_reconciliation",
        fromlist=["_lock_meta_graph"],
    )
    original_lock_graph = module._lock_meta_graph

    def coordinated_lock_graph(session, reconciliation, *, accepting=False):
        graph = original_lock_graph(session, reconciliation, accepting=accepting)
        if reconciliation.id == first.reconciliation_id:
            ingress_has_first_graph.set()
            assert scanner_has_second_graph.wait(timeout=10)
        elif reconciliation.id == second.reconciliation_id:
            scanner_has_second_graph.set()
        return graph

    monkeypatch.setattr(module, "_lock_meta_graph", coordinated_lock_graph)

    def ingress_order():
        return reconcile_meta_callback_outcome(
            Session,
            reconciliation_id=first.reconciliation_id,
            now=max(first.deadline_at, second.deadline_at) + timedelta(seconds=1),
        ).decision

    def scanner_order():
        assert ingress_has_first_graph.wait(timeout=10)
        return reconcile_due_meta_callback_outcomes(
            Session,
            worker_id="v2-concurrent-scanner",
            now=max(first.closure_after, second.closure_after),
            limit=1,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        ingress_future = pool.submit(ingress_order)
        scanner_future = pool.submit(scanner_order)
        ingress_decision = ingress_future.result(timeout=15)
        scanner_result = scanner_future.result(timeout=15)
    assert ingress_decision == "PASSED"
    assert scanner_result.failed == 0
    with Session() as session:
        for attempt in (first, second):
            assert session.get(ScenarioRunRow, attempt.run_id).status == "PASSED"
            assert _terminal_event_count(session, attempt) == 1
        assert session.scalar(
            select(func.count()).select_from(EffectConsumptionRow).where(
                EffectConsumptionRow.execution_intent_id.in_(
                    [first.agent_id, second.agent_id]
                )
            )
        ) == 2
        assert session.scalar(
            select(func.count()).select_from(OutboxMessageRow).where(
                OutboxMessageRow.execution_intent_id.in_([first.agent_id, second.agent_id])
            )
        ) == 2
        assert session.execute(select(func.count())).scalar_one() == 1
        assert session.scalar(
            text(
                "select count(*) from pg_stat_activity "
                "where datname = current_database() "
                "and state = 'idle in transaction (aborted)'"
            )
        ) == 0


def test_worker_rolls_back_one_row_and_continues(monkeypatch, Session):
    first = _accepted_attempt(Session, suffix="1" + uuid4().hex[1:])
    second = _accepted_attempt(Session, suffix="f" + uuid4().hex[1:])
    original = __import__(
        "attention_router.platform.meta_callback_reconciliation",
        fromlist=["_reconcile_locked_meta_callback_outcome"],
    )._reconcile_locked_meta_callback_outcome

    def fail_first(session, reconciliation, *, now):
        if reconciliation.id == min(first.reconciliation_id, second.reconciliation_id):
            raise RuntimeError("injected row failure")
        return original(session, reconciliation, now=now)

    monkeypatch.setattr(
        "attention_router.platform.meta_callback_reconciliation."
        "_reconcile_locked_meta_callback_outcome",
        fail_first,
    )
    sweep = reconcile_due_meta_callback_outcomes(
        Session,
        worker_id="v2-row-isolation",
        now=max(first.closure_after, second.closure_after),
        limit=2,
    )
    assert (sweep.selected, sweep.processed, sweep.failed) == (2, 1, 1)
    with Session() as session:
        statuses = {
            session.get(ScenarioRunRow, first.run_id).status,
            session.get(ScenarioRunRow, second.run_id).status,
        }
        assert statuses == {"RUNNING", "FAILED"}
        assert session.scalar(
            select(func.count()).select_from(AuditEventRow).where(
                AuditEventRow.event_type
                == "production_meta_reconciliation_row_failed"
            )
        ) >= 1
        assert session.scalar(
            select(func.count()).select_from(MetaCallbackEvidenceRow).where(
                MetaCallbackEvidenceRow.reconciliation_id.in_(
                    [first.reconciliation_id, second.reconciliation_id]
                )
            )
        ) == 0
    monkeypatch.setattr(
        "attention_router.platform.meta_callback_reconciliation."
        "_reconcile_locked_meta_callback_outcome",
        original,
    )
    failed_attempt = first
    with Session() as session:
        if session.get(ScenarioRunRow, second.run_id).status == "RUNNING":
            failed_attempt = second
    reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=failed_attempt.reconciliation_id,
        now=max(first.closure_after, second.closure_after),
    )


def test_two_workers_skip_locked_prevent_double_terminal(Session):
    attempt = _accepted_attempt(Session)
    barrier = Barrier(2)

    def worker(worker_id):
        barrier.wait()
        return reconcile_due_meta_callback_outcomes(
            Session,
            worker_id=worker_id,
            now=attempt.closure_after,
            limit=1,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker, "worker-a")
        second = pool.submit(worker, "worker-b")
        results = [first.result(timeout=15), second.result(timeout=15)]
    assert sum(result.failed for result in results) == 0
    assert sum(result.terminalized for result in results) == 1
    with Session() as session:
        assert session.get(ScenarioRunRow, attempt.run_id).status == "FAILED"
        assert _terminal_event_count(session, attempt) == 1
        assert session.get(OutboxMessageRow, attempt.outbox_id).attempt_count == 1
        assert session.scalar(
            select(func.count()).select_from(EffectConsumptionRow).where(
                EffectConsumptionRow.execution_intent_id == attempt.agent_id
            )
        ) == 1


def test_two_bounded_workers_drain_all_rows_without_duplicate_or_starvation(Session):
    attempts = [_accepted_attempt(Session) for _ in range(4)]
    barrier = Barrier(2)
    now = max(attempt.closure_after for attempt in attempts)

    def worker(worker_id):
        barrier.wait()
        return reconcile_due_meta_callback_outcomes(
            Session,
            worker_id=worker_id,
            now=now,
            limit=2,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker, "bounded-worker-a")
        second = pool.submit(worker, "bounded-worker-b")
        results = [first.result(timeout=20), second.result(timeout=20)]
    assert sum(result.selected for result in results) == 4
    assert sum(result.terminalized for result in results) == 4
    assert sum(result.failed for result in results) == 0
    with Session() as session:
        for attempt in attempts:
            assert session.get(ScenarioRunRow, attempt.run_id).status == "FAILED"
            assert _terminal_event_count(session, attempt) == 1


@pytest.mark.parametrize("terminal", ["PASSED", "FAILED"])
def test_terminal_replay_preserves_complete_persistent_graph(Session, terminal):
    attempt = _accepted_attempt(Session)
    if terminal == "PASSED":
        admit_meta_callback_evidence(
            Session,
            status_event=_status(attempt, "read", 1_788_220_800),
            received_at=attempt.deadline_at,
        )
        terminal_at = attempt.deadline_at + timedelta(seconds=1)
    else:
        terminal_at = attempt.closure_after
    first = reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=terminal_at,
    )
    assert first.decision == terminal
    with Session() as session:
        before = _terminal_graph_snapshot(session, attempt)
    replay = reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=terminal_at + timedelta(seconds=60),
    )
    assert replay.state_changed is False
    assert replay.domain_state_changed is False
    with Session() as session:
        after = _terminal_graph_snapshot(session, attempt)
    assert after == before
    with Session() as session:
        with pytest.raises(
            ProductionAuthorityDenied, match="PRODUCTION_APPROVED_GRAPH_INVALID"
        ):
            reserve_production_conversation_reply(
                session,
                agent_execution_intent_id=attempt.agent_id,
                authorization_id=attempt.authorization_id,
                dispatcher_id="forbidden-retry",
                now=terminal_at + timedelta(seconds=61),
            )
        session.rollback()
    with Session() as session:
        assert _terminal_graph_snapshot(session, attempt) == before


def test_pre_acceptance_callback_is_durable_and_correlates_on_acceptance(Session):
    reserved = _reserved_attempt(Session)
    callback = {
        "id": reserved.provider_message_id,
        "status": "delivered",
        "timestamp": 1_788_220_800,
        "errors": [],
    }
    admission = admit_meta_callback_evidence(
        Session,
        status_event=callback,
        received_at=reserved.reserved_at,
    )
    assert admission.evidence_persisted is True
    assert admission.correlation_pending is True
    with Session() as session:
        inbox = session.get(MetaCallbackInboxRow, admission.inbox_id)
        assert inbox is not None and inbox.state == "PENDING"
        assert session.scalar(
            select(func.count()).select_from(MetaCallbackEvidenceRow).where(
                MetaCallbackEvidenceRow.inbox_id == admission.inbox_id
            )
        ) == 0

    with Session() as session:
        mark_production_meta_api_accepted(
            session,
            outbox_message_id=reserved.outbox_id,
            dispatcher_id="v2-test-dispatcher",
            provider_message_id=reserved.provider_message_id,
            now=reserved.reserved_at + timedelta(seconds=1),
        )
        session.commit()

    with Session() as session:
        inbox = session.get(MetaCallbackInboxRow, admission.inbox_id)
        reconciliation = session.scalar(
            select(MetaDeliveryReconciliationRow).where(
                MetaDeliveryReconciliationRow.provider_message_id
                == reserved.provider_message_id
            )
        )
        assert inbox is not None and inbox.state == "CORRELATED"
        assert inbox.reconciliation_id == reconciliation.id
        assert session.scalar(
            select(func.count()).select_from(MetaCallbackEvidenceRow).where(
                MetaCallbackEvidenceRow.inbox_id == inbox.id
            )
        ) == 1
        assert session.get(ScenarioRunRow, reserved.run_id).status == "PASSED"
        assert session.get(OutboxMessageRow, reserved.outbox_id).attempt_count == 1


def test_identical_replay_is_write_inert_after_first_durable_receipt(Session):
    attempt = _accepted_attempt(Session)
    callback = _status(attempt, "delivered", 1_788_220_800)
    admit_meta_callback_evidence(
        Session, status_event=callback, received_at=attempt.deadline_at
    )
    for _ in range(5):
        replay = admit_meta_callback_evidence(
            Session, status_event=callback, received_at=attempt.deadline_at
        )
        assert replay.duplicate is True
        assert replay.evidence_persisted is False
    reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.deadline_at + timedelta(seconds=1),
    )
    with Session() as session:
        assert session.scalar(
            select(func.count()).select_from(MetaCallbackInboxRow).where(
                MetaCallbackInboxRow.provider_message_id == attempt.provider_message_id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(MetaCallbackEvidenceRow).where(
                MetaCallbackEvidenceRow.reconciliation_id == attempt.reconciliation_id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(AuditEventRow).where(
                AuditEventRow.event_type == "meta_status_received",
                AuditEventRow.payload["provider_message_id_hash"].as_string()
                == hashlib.sha256(attempt.provider_message_id.encode()).hexdigest(),
            )
        ) == 1
        assert _terminal_event_count(session, attempt) == 1


@pytest.mark.parametrize("first_status", ["delivered", "failed"])
def test_delivery_failure_conflict_marker_converges_once(Session, first_status):
    attempt = _accepted_attempt(Session)
    second_status = "failed" if first_status == "delivered" else "delivered"
    for status_name in (first_status, second_status):
        admit_meta_callback_evidence(
            Session,
            status_event=_status(attempt, status_name, 1_788_220_800),
            received_at=attempt.deadline_at,
        )
        reconcile_meta_callback_outcome(
            Session,
            reconciliation_id=attempt.reconciliation_id,
            now=attempt.deadline_at + timedelta(seconds=1),
        )
    with Session() as session:
        reconciliation = session.get(
            MetaDeliveryReconciliationRow, attempt.reconciliation_id
        )
        assert reconciliation.state == "PASSED"
        assert session.scalar(
            select(func.count()).select_from(MetaCallbackAuditMarkerRow).where(
                MetaCallbackAuditMarkerRow.reconciliation_id
                == attempt.reconciliation_id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(AuditEventRow).where(
                AuditEventRow.event_type
                == "production_meta_delivery_failure_conflict",
                AuditEventRow.payload["outbox_id"].as_string() == attempt.outbox_id,
            )
        ) == 1
        assert _terminal_event_count(session, attempt) == 1


def test_poison_row_has_durable_backoff_quarantine_and_explicit_recovery(
    monkeypatch, Session
):
    poison = _accepted_attempt(Session)
    valid = _accepted_attempt(Session)
    module = __import__(
        "attention_router.platform.meta_callback_reconciliation",
        fromlist=["_reconcile_locked_meta_callback_outcome"],
    )
    original = module._reconcile_locked_meta_callback_outcome

    def fail_poison(session, reconciliation, *, now):
        if reconciliation.id == poison.reconciliation_id:
            raise RuntimeError("deterministic poison")
        return original(session, reconciliation, now=now)

    monkeypatch.setattr(module, "_reconcile_locked_meta_callback_outcome", fail_poison)
    first_now = max(poison.closure_after, valid.closure_after)
    first = reconcile_due_meta_callback_outcomes(
        Session, worker_id="bounded-poison", now=first_now, limit=2
    )
    assert (first.failed, first.processed) == (1, 1)
    same_cycle = reconcile_due_meta_callback_outcomes(
        Session, worker_id="bounded-poison", now=first_now, limit=1
    )
    assert same_cycle.selected == 0

    for expected_count in range(2, 6):
        with Session() as session:
            row = session.get(
                MetaDeliveryReconciliationRow, poison.reconciliation_id
            )
            next_attempt = row.next_reconcile_at
        reconcile_due_meta_callback_outcomes(
            Session,
            worker_id="bounded-poison",
            now=next_attempt,
            limit=1,
        )
        with Session() as session:
            row = session.get(
                MetaDeliveryReconciliationRow, poison.reconciliation_id
            )
            assert row.failure_count == expected_count

    with Session() as session:
        row = session.get(MetaDeliveryReconciliationRow, poison.reconciliation_id)
        assert row.operational_state == "QUARANTINED"
        assert session.scalar(
            select(func.count()).select_from(AuditEventRow).where(
                AuditEventRow.event_type
                == "production_meta_reconciliation_row_failed",
                AuditEventRow.payload["reconciliation_id"].as_string()
                == poison.reconciliation_id,
            )
        ) == 5
        assert recover_quarantined_meta_reconciliation(
            session,
            reconciliation_id=poison.reconciliation_id,
            operator_id="test-operator",
            reason="REVIEWED_SAFE_RETRY",
            now=first_now + timedelta(minutes=10),
        )
        session.commit()
    with Session() as session:
        row = session.get(MetaDeliveryReconciliationRow, poison.reconciliation_id)
        assert row.operational_state == "ACTIVE"
        assert row.failure_count == 0


def test_more_than_batch_of_poison_rows_cannot_starve_following_valid_row(
    monkeypatch, Session
):
    poisons = [_accepted_attempt(Session) for _ in range(101)]
    valid = _accepted_attempt(Session)
    poison_ids = {attempt.reconciliation_id for attempt in poisons}
    due_at = max(
        [attempt.closure_after for attempt in poisons] + [valid.closure_after]
    )
    with Session() as session:
        for attempt in poisons:
            row = session.get(
                MetaDeliveryReconciliationRow, attempt.reconciliation_id
            )
            row.next_reconcile_at = due_at
        valid_row = session.get(
            MetaDeliveryReconciliationRow, valid.reconciliation_id
        )
        valid_row.next_reconcile_at = due_at + timedelta(microseconds=1)
        session.commit()

    module = __import__(
        "attention_router.platform.meta_callback_reconciliation",
        fromlist=["_reconcile_locked_meta_callback_outcome"],
    )
    original = module._reconcile_locked_meta_callback_outcome

    def fail_poison(session, reconciliation, *, now):
        if reconciliation.id in poison_ids:
            raise RuntimeError("deterministic poison prefix")
        return original(session, reconciliation, now=now)

    monkeypatch.setattr(module, "_reconcile_locked_meta_callback_outcome", fail_poison)
    first = reconcile_due_meta_callback_outcomes(
        Session,
        worker_id="poison-prefix-1",
        now=due_at + timedelta(microseconds=1),
        limit=100,
    )
    assert (first.selected, first.failed, first.processed) == (100, 100, 0)
    second = reconcile_due_meta_callback_outcomes(
        Session,
        worker_id="poison-prefix-2",
        now=due_at + timedelta(microseconds=1),
        limit=2,
    )
    assert (second.selected, second.failed, second.processed) == (2, 1, 1)
    with Session() as session:
        assert session.get(ScenarioRunRow, valid.run_id).status == "FAILED"
        assert _terminal_event_count(session, valid) == 1
        assert session.scalar(
            select(func.count()).select_from(MetaDeliveryReconciliationRow).where(
                MetaDeliveryReconciliationRow.id.in_(poison_ids),
                MetaDeliveryReconciliationRow.failure_count == 1,
            )
        ) == 101


def test_attempt_gate_serializes_duplicate_reservation_before_api_acceptance(
    monkeypatch, Session
):
    reserved = _reserved_attempt(Session)
    gate_held = Event()
    release_gate = Event()
    production_module = __import__(
        "attention_router.platform.production_controlled_execution",
        fromlist=["acquire_meta_attempt_gate"],
    )
    original_gate = production_module.acquire_meta_attempt_gate

    def hold_duplicate_gate(session, outbox_id):
        original_gate(session, outbox_id)
        gate_held.set()
        assert release_gate.wait(timeout=10)

    monkeypatch.setattr(
        production_module, "acquire_meta_attempt_gate", hold_duplicate_gate
    )

    def duplicate_reservation():
        with Session() as session:
            try:
                reserve_production_conversation_reply(
                    session,
                    agent_execution_intent_id=reserved.agent_id,
                    authorization_id=reserved.authorization_id,
                    dispatcher_id="duplicate-dispatcher",
                    now=reserved.reserved_at + timedelta(seconds=1),
                )
                session.commit()
                return "unexpected-success"
            except Exception as exc:
                session.rollback()
                assert session.scalar(text("SELECT 1")) == 1
                return str(exc)

    def acceptance():
        with Session() as session:
            mark_production_meta_api_accepted(
                session,
                outbox_message_id=reserved.outbox_id,
                dispatcher_id="v2-test-dispatcher",
                provider_message_id=reserved.provider_message_id,
                now=reserved.reserved_at + timedelta(seconds=1),
            )
            session.commit()
            assert session.scalar(text("SELECT 1")) == 1
            return "accepted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        duplicate_future = pool.submit(duplicate_reservation)
        assert gate_held.wait(timeout=10)
        acceptance_future = pool.submit(acceptance)
        release_gate.set()
        duplicate_result = duplicate_future.result(timeout=15)
        acceptance_result = acceptance_future.result(timeout=15)
    assert duplicate_result in {
        "PRODUCTION_OUTBOX_ALREADY_EXISTS_NO_RETRY",
        "PRODUCTION_RUN_NOT_ARMED",
    }
    assert acceptance_result == "accepted"
    with Session() as session:
        outbox = session.get(OutboxMessageRow, reserved.outbox_id)
        assert outbox.status == "AWAITING_DELIVERY"
        assert outbox.attempt_count == 1
        assert session.scalar(
            select(func.count()).select_from(OutboxMessageRow).where(
                OutboxMessageRow.execution_intent_id == reserved.agent_id
            )
        ) == 1


def test_unmatched_early_inbox_has_bounded_backoff_and_quarantine(Session):
    received_at = datetime.now(UTC)
    provider_id = f"wamid.v2.unmatched.{uuid4().hex}"
    admission = admit_meta_callback_evidence(
        Session,
        status_event={
            "id": provider_id,
            "status": "delivered",
            "timestamp": 1_788_220_800,
            "errors": [],
        },
        received_at=received_at,
    )
    assert admission.correlation_pending is True
    with Session() as session:
        current = session.get(
            MetaCallbackInboxRow, admission.inbox_id
        ).next_correlation_at
    for expected in range(1, 6):
        sweep = reconcile_pending_meta_callback_inbox(
            Session,
            worker_id="inbox-bounded",
            now=current,
            limit=1,
        )
        assert sweep.selected == 1
        with Session() as session:
            inbox = session.get(MetaCallbackInboxRow, admission.inbox_id)
            assert inbox.correlation_attempt_count == expected
            if expected < 5:
                assert inbox.state == "PENDING"
                same_time = reconcile_pending_meta_callback_inbox(
                    Session,
                    worker_id="inbox-bounded",
                    now=current,
                    limit=1,
                )
                assert same_time.selected == 0
                current = inbox.next_correlation_at
            else:
                assert inbox.state == "QUARANTINED"
    with Session() as session:
        assert session.scalar(
            select(func.count()).select_from(AuditEventRow).where(
                AuditEventRow.event_type
                == "production_meta_callback_correlation_deferred",
                AuditEventRow.payload["inbox_id"].as_string() == admission.inbox_id,
            )
        ) == 5


def test_database_rejects_evidence_mutation_and_terminal_reconciliation_update(Session):
    attempt = _accepted_attempt(Session)
    admit_meta_callback_evidence(
        Session,
        status_event=_status(attempt, "delivered", 1_788_220_800),
        received_at=attempt.deadline_at,
    )
    reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.deadline_at + timedelta(seconds=1),
    )
    with Session() as session:
        before = _terminal_graph_snapshot(session, attempt)
        evidence_id = session.scalar(
            select(MetaCallbackEvidenceRow.id).where(
                MetaCallbackEvidenceRow.reconciliation_id == attempt.reconciliation_id
            )
        )
        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(
                text(
                    "update meta_callback_evidence set provider_status='sent' "
                    "where id=:id"
                ),
                {"id": evidence_id},
            )
            session.flush()
        session.rollback()
    with Session() as session:
        with pytest.raises(DBAPIError, match="terminal Meta reconciliation is immutable"):
            session.execute(
                text(
                    "update meta_delivery_reconciliations set updated_at=now() "
                    "where id=:id"
                ),
                {"id": attempt.reconciliation_id},
            )
            session.flush()
        session.rollback()
    with Session() as session:
        after = _terminal_graph_snapshot(session, attempt)
    assert after == before


def test_worker_is_db_only_and_creates_no_external_effect_or_new_authority(
    monkeypatch, Session
):
    attempt = _accepted_attempt(Session)

    def forbidden(*args, **kwargs):
        raise AssertionError("external adapter invoked by DB-only reconciler")

    monkeypatch.setattr(
        "attention_router.adapters.meta_cloud_outbound.MetaCloudOutboundAdapter.dispatch_outbox",
        forbidden,
    )
    monkeypatch.setattr(
        "attention_router.adapters.wwebjs_outbound.WwebjsOutboundAdapter.dispatch_outbox",
        forbidden,
    )
    with Session() as session:
        before = {
            "outboxes": session.scalar(select(func.count()).select_from(OutboxMessageRow)),
            "budgets": session.scalar(select(func.count()).select_from(EffectBudgetRow)),
            "leases": session.scalar(select(func.count()).select_from(ExecutionLeaseRow)),
            "consumptions": session.scalar(
                select(func.count()).select_from(EffectConsumptionRow)
            ),
            "bounded": session.scalar(
                select(func.count()).select_from(BoundedRunAuthorizationRow)
            ),
            "authorizations": session.scalar(
                select(func.count()).select_from(HumanExecutionAuthorizationRow)
            ),
        }
    sweep = reconcile_due_meta_callback_outcomes(
        Session,
        worker_id="v2-db-only-proof",
        now=attempt.closure_after,
        limit=1,
    )
    assert (sweep.processed, sweep.terminalized, sweep.failed) == (1, 1, 0)
    with Session() as session:
        after = {
            "outboxes": session.scalar(select(func.count()).select_from(OutboxMessageRow)),
            "budgets": session.scalar(select(func.count()).select_from(EffectBudgetRow)),
            "leases": session.scalar(select(func.count()).select_from(ExecutionLeaseRow)),
            "consumptions": session.scalar(
                select(func.count()).select_from(EffectConsumptionRow)
            ),
            "bounded": session.scalar(
                select(func.count()).select_from(BoundedRunAuthorizationRow)
            ),
            "authorizations": session.scalar(
                select(func.count()).select_from(HumanExecutionAuthorizationRow)
            ),
        }
    assert after == before
