from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import logging
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from attention_router.infrastructure.models import (
    AuditEventRow,
    ExecutionIntentRow,
    HumanExecutionAuthorizationRow,
    MetaCallbackEvidenceRow,
    MetaCallbackInboxRow,
    MetaDeliveryReconciliationRow,
    ScenarioRunRow,
)
from attention_router.infrastructure.repository import seed_policies
from attention_router.platform.execution_safety import SafetyDenied
from attention_router.platform.human_approval_integration import (
    close_failed_production_human_approval,
)
from attention_router.platform.human_execution_authorization import (
    fingerprint,
    prepare,
    request_approval,
)
from attention_router.platform.meta_callback_reconciliation import (
    admit_meta_callback_evidence,
    recover_quarantined_meta_callback_inbox,
    recover_quarantined_meta_reconciliation,
    reconcile_meta_callback_outcome,
    reconcile_pending_meta_callback_inbox,
)
from attention_router.platform.production_controlled_execution import (
    mark_production_meta_api_accepted,
)
from attention_router.web.ingress_app import (
    app,
    get_callback_session_factory,
    get_session,
)
from tests.integration.test_postgres_meta import post_signed
from tests.integration.test_postgres_meta_callback_reconciliation_v2 import (
    _accepted_attempt,
    _reserved_attempt,
)
from tests.meta_fixtures import APP_SECRET, meta_status


pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def signed_client(Session, monkeypatch):
    monkeypatch.setattr(
        "attention_router.web.ingress_app.settings.meta_app_secret", APP_SECRET
    )
    monkeypatch.setattr(
        "attention_router.web.ingress_app.settings.meta_webhook_dispatch_enabled",
        True,
    )

    def override():
        session = Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_session] = override
    app.dependency_overrides[get_callback_session_factory] = lambda: Session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _callback(provider_id: str, status: str = "delivered") -> dict:
    return {
        "id": provider_id,
        "status": status,
        "timestamp": 1_788_220_800,
        "errors": [],
    }


def _quarantine_reconciliation(Session, attempt) -> datetime:
    timestamp = attempt.accepted_at + timedelta(seconds=1)
    with Session() as session:
        row = session.get(MetaDeliveryReconciliationRow, attempt.reconciliation_id)
        row.operational_state = "QUARANTINED"
        row.failure_count = 5
        row.last_failure_at = timestamp
        row.last_failure_code = "SyntheticFailure"
        row.quarantined_at = timestamp
        row.quarantine_reason = "MAX_RECONCILIATION_FAILURES"
        session.commit()
    return timestamp


def _park_other_pending_inbox(Session, *, keep_id: str | None = None) -> None:
    with Session.begin() as session:
        query = update(MetaCallbackInboxRow).where(
            MetaCallbackInboxRow.state == "PENDING"
        )
        if keep_id is not None:
            query = query.where(MetaCallbackInboxRow.id != keep_id)
        session.execute(
            query.values(next_correlation_at=datetime(2100, 1, 1, tzinfo=UTC))
        )


def _alembic(url: str, action: str, revision: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    subprocess.run(
        [sys.executable, "-m", "alembic", action, revision],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_early_callback_quarantine_is_requeued_only_by_exact_acceptance(Session):
    reserved = _reserved_attempt(Session, suffix=uuid4().hex)
    admission = admit_meta_callback_evidence(
        Session,
        status_event=_callback(reserved.provider_message_id),
        received_at=reserved.reserved_at,
    )
    _park_other_pending_inbox(Session, keep_id=admission.inbox_id)
    with Session() as session:
        current = session.get(
            MetaCallbackInboxRow, admission.inbox_id
        ).next_correlation_at
    for _ in range(5):
        reconcile_pending_meta_callback_inbox(
            Session, worker_id="release-gate-early", now=current, limit=1
        )
        with Session() as session:
            inbox = session.get(MetaCallbackInboxRow, admission.inbox_id)
            current = inbox.next_correlation_at
    with Session() as session:
        inbox = session.get(MetaCallbackInboxRow, admission.inbox_id)
        assert inbox.state == "QUARANTINED"
        assert inbox.last_error_code == "PRODUCTION_META_RECONCILIATION_NOT_FOUND"
        mark_production_meta_api_accepted(
            session,
            outbox_message_id=reserved.outbox_id,
            dispatcher_id="v2-test-dispatcher",
            provider_message_id=reserved.provider_message_id,
            now=reserved.reserved_at + timedelta(seconds=1),
        )
        session.commit()
    replay = admit_meta_callback_evidence(
        Session,
        status_event=_callback(reserved.provider_message_id),
        received_at=reserved.reserved_at + timedelta(seconds=2),
    )
    assert replay.duplicate is True
    assert replay.evidence_persisted is False
    with Session() as session:
        inbox = session.get(MetaCallbackInboxRow, admission.inbox_id)
        assert inbox.state == "CORRELATED"
        assert session.scalar(
            select(func.count()).select_from(MetaCallbackEvidenceRow).where(
                MetaCallbackEvidenceRow.inbox_id == inbox.id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(AuditEventRow).where(
                AuditEventRow.event_type
                == "production_meta_callback_exact_acceptance_requeued",
                AuditEventRow.payload["inbox_id"].as_string() == inbox.id,
            )
        ) == 1
        assert session.get(ScenarioRunRow, reserved.run_id).status == "PASSED"


def test_exact_quarantine_cannot_be_updated_without_evidence_and_audit(Session):
    attempt = _accepted_attempt(Session, suffix=uuid4().hex)
    inbox_id = "guarded-quarantine-" + uuid4().hex
    received_at = attempt.accepted_at - timedelta(seconds=1)
    with Session.begin() as session:
        session.add(
            MetaCallbackInboxRow(
                id=inbox_id,
                provider_message_id=attempt.provider_message_id,
                deduplication_key=uuid4().hex.ljust(64, "0"),
                provider_status="delivered",
                provider_timestamp_raw="1788220800",
                provider_timestamp=datetime.fromtimestamp(1_788_220_800, tz=UTC),
                received_at=received_at,
                valid=True,
                errors_present=False,
                error_fingerprint=None,
                state="QUARANTINED",
                reconciliation_id=None,
                correlation_attempt_count=5,
                next_correlation_at=received_at,
                last_error_code="PRODUCTION_META_RECONCILIATION_NOT_FOUND",
                correlated_at=None,
                quarantined_at=received_at,
                claim_token=None,
                claimed_by=None,
                claimed_at=None,
                last_requeue_audit_id=None,
                created_at=received_at,
                updated_at=received_at,
            )
        )
    with pytest.raises(DBAPIError):
        with Session.begin() as session:
            session.execute(
                update(MetaCallbackInboxRow)
                .where(MetaCallbackInboxRow.id == inbox_id)
                .values(
                    state="CORRELATED",
                    reconciliation_id=attempt.reconciliation_id,
                    correlated_at=attempt.accepted_at,
                    quarantined_at=None,
                    last_error_code=None,
                )
            )
    with Session() as session:
        inbox = session.get(MetaCallbackInboxRow, inbox_id)
        assert inbox.state == "QUARANTINED"
        assert session.scalar(
            select(func.count()).select_from(MetaCallbackEvidenceRow).where(
                MetaCallbackEvidenceRow.inbox_id == inbox_id
            )
        ) == 0


def test_0029_quarantined_early_callback_is_recovered_by_0030(pg_url):
    database = f"attention_router_early_q_{uuid4().hex[:10]}"
    base = pg_url.rsplit("/", 1)[0]
    admin = create_engine(
        f"{base}/postgres", isolation_level="AUTOCOMMIT", future=True
    )
    url = f"{base}/{database}"
    with admin.connect() as connection:
        connection.execute(text(f'create database "{database}"'))
    try:
        _alembic(url, "upgrade", "head")
        engine = create_engine(url, future=True)
        IsolatedSession = sessionmaker(
            bind=engine, expire_on_commit=False, future=True
        )
        with IsolatedSession() as session:
            seed_policies(session)
            session.commit()
        attempt = _accepted_attempt(IsolatedSession, suffix=uuid4().hex)
        module = __import__(
            "attention_router.platform.meta_callback_reconciliation",
            fromlist=["_evidence_deduplication_key"],
        )
        received_at = attempt.accepted_at - timedelta(seconds=1)
        inbox_id = "historical-early-inbox-" + uuid4().hex
        with IsolatedSession.begin() as session:
            session.add(
                MetaCallbackInboxRow(
                    id=inbox_id,
                    provider_message_id=attempt.provider_message_id,
                    deduplication_key=module._evidence_deduplication_key(
                        provider_status="delivered",
                        provider_timestamp_raw=1_788_220_800,
                        error_fingerprint=None,
                    ),
                    provider_status="delivered",
                    provider_timestamp_raw="1788220800",
                    provider_timestamp=datetime.fromtimestamp(
                        1_788_220_800, tz=UTC
                    ),
                    received_at=received_at,
                    valid=True,
                    errors_present=False,
                    error_fingerprint=None,
                    state="QUARANTINED",
                    reconciliation_id=None,
                    correlation_attempt_count=5,
                    next_correlation_at=attempt.accepted_at,
                    last_error_code=(
                        "PRODUCTION_META_RECONCILIATION_NOT_FOUND"
                    ),
                    correlated_at=None,
                    quarantined_at=attempt.accepted_at,
                    claim_token=None,
                    claimed_by=None,
                    claimed_at=None,
                    last_requeue_audit_id=None,
                    created_at=received_at,
                    updated_at=attempt.accepted_at,
                )
            )
        engine.dispose()

        _alembic(url, "downgrade", "0029_meta_callback_remediation")
        _alembic(url, "upgrade", "head")
        _alembic(url, "upgrade", "head")

        engine = create_engine(url, future=True)
        IsolatedSession.configure(bind=engine)
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT state, reconciliation_id FROM meta_callback_inbox "
                    "WHERE id = :id"
                ),
                {"id": inbox_id},
            ).one()
            assert row == ("CORRELATED", attempt.reconciliation_id)
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM meta_callback_evidence "
                    "WHERE inbox_id = :id"
                ),
                {"id": inbox_id},
            ) == 1
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE event_type = "
                    "'production_meta_callback_exact_acceptance_requeued' "
                    "AND payload->>'inbox_id' = :id"
                ),
                {"id": inbox_id},
            ) == 1
        result = reconcile_meta_callback_outcome(
            IsolatedSession,
            reconciliation_id=attempt.reconciliation_id,
            now=attempt.accepted_at,
        )
        assert result.decision == "PASSED"
        engine.dispose()
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'drop database if exists "{database}" with (force)'))
        admin.dispose()


def test_two_workers_claim_one_due_inbox_once_with_skip_locked(Session):
    admission = admit_meta_callback_evidence(
        Session,
        status_event=_callback("claim-race-" + uuid4().hex),
        received_at=datetime.now(UTC),
    )
    _park_other_pending_inbox(Session, keep_id=admission.inbox_id)
    barrier = Barrier(2)
    engine = Session.kw["bind"]

    def coordinate_selection(
        conn, cursor, statement, parameters, context, executemany
    ):
        normalized = " ".join(statement.lower().split())
        is_selection = (
            normalized.startswith("select meta_callback_inbox.id")
            and "meta_callback_inbox.next_correlation_at" in normalized
            and "order by meta_callback_inbox.next_correlation_at" in normalized
            and "skip locked" in normalized
        )
        if is_selection:
            barrier.wait(timeout=10)

    event.listen(engine, "after_cursor_execute", coordinate_selection)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    reconcile_pending_meta_callback_inbox,
                    Session,
                    worker_id=f"claim-worker-{index}",
                    now=datetime.now(UTC),
                    limit=1,
                )
                for index in range(2)
            ]
            results = [future.result(timeout=20) for future in futures]
    finally:
        event.remove(engine, "after_cursor_execute", coordinate_selection)
    assert sum(result.selected for result in results) == 1
    with Session() as session:
        inbox = session.get(MetaCallbackInboxRow, admission.inbox_id)
        assert inbox.correlation_attempt_count == 1
        assert inbox.claim_token is None
        assert session.scalar(
            select(func.count()).select_from(AuditEventRow).where(
                AuditEventRow.event_type
                == "production_meta_callback_correlation_deferred",
                AuditEventRow.payload["inbox_id"].as_string() == inbox.id,
            )
        ) == 1
        inbox.next_correlation_at = datetime(2100, 1, 1, tzinfo=UTC)
        session.commit()


def test_acceptance_correlates_at_most_100_inbox_rows_without_evidence_n_plus_one(
    Session,
):
    reserved = _reserved_attempt(Session, suffix=uuid4().hex)
    _insert_pending_inbox_batch(Session, reserved, ["delivered"] * 101)

    statements: list[str] = []
    engine = Session.kw["bind"]

    def count_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", count_sql)
    try:
        with Session.begin() as session:
            mark_production_meta_api_accepted(
                session,
                outbox_message_id=reserved.outbox_id,
                dispatcher_id="v2-test-dispatcher",
                provider_message_id=reserved.provider_message_id,
                now=reserved.reserved_at + timedelta(seconds=1),
            )
    finally:
        event.remove(engine, "before_cursor_execute", count_sql)

    evidence_lookups = [
        statement
        for statement in statements
        if statement.startswith("select")
        and " from meta_callback_evidence" in statement
    ]
    assert len(statements) < 80
    assert len(evidence_lookups) <= 3
    with Session() as session:
        reconciliation_id = _accepted_reconciliation_id(
            session, reserved.outbox_id
        )
        assert session.get(
            MetaDeliveryReconciliationRow, reconciliation_id
        ).state == "PENDING"
        assert session.scalar(
            select(func.count()).select_from(MetaCallbackEvidenceRow).where(
                MetaCallbackEvidenceRow.reconciliation_id
                == reconciliation_id
            )
        ) == 100
        assert session.scalar(
            select(func.count()).select_from(MetaCallbackInboxRow).where(
                MetaCallbackInboxRow.provider_message_id
                == reserved.provider_message_id,
                MetaCallbackInboxRow.state == "PENDING",
            )
        ) == 1

    sweep = reconcile_pending_meta_callback_inbox(
        Session,
        worker_id="bounded-tail-worker",
        now=reserved.reserved_at + timedelta(seconds=2),
        limit=1,
    )
    assert sweep.correlated == 1
    with Session() as session:
        reconciliation_id = _accepted_reconciliation_id(
            session, reserved.outbox_id
        )
        assert session.scalar(
            select(func.count()).select_from(MetaCallbackEvidenceRow).where(
                MetaCallbackEvidenceRow.reconciliation_id == reconciliation_id
            )
        ) == 101
    assert reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=reconciliation_id,
        now=reserved.reserved_at + timedelta(seconds=2),
    ).decision == "PASSED"


def _accepted_reconciliation_id(session, outbox_id: str) -> str:
    return session.scalar(
        select(MetaDeliveryReconciliationRow.id).where(
            MetaDeliveryReconciliationRow.outbox_message_id == outbox_id
        )
    )


def _insert_pending_inbox_batch(Session, reserved, statuses: list[str]) -> None:
    with Session.begin() as session:
        for index, provider_status in enumerate(statuses):
            received_at = reserved.reserved_at + timedelta(microseconds=index)
            session.add(
                MetaCallbackInboxRow(
                    id=f"bounded-inbox-{uuid4().hex}",
                    provider_message_id=reserved.provider_message_id,
                    deduplication_key=f"{index:064x}",
                    provider_status=provider_status,
                    provider_timestamp_raw=str(1_788_220_800 + index),
                    provider_timestamp=datetime.fromtimestamp(
                        1_788_220_800 + index, tz=UTC
                    ),
                    received_at=received_at,
                    valid=True,
                    errors_present=provider_status == "failed",
                    error_fingerprint=(
                        f"{index:064x}" if provider_status == "failed" else None
                    ),
                    state="PENDING",
                    reconciliation_id=None,
                    correlation_attempt_count=0,
                    next_correlation_at=received_at,
                    last_error_code=None,
                    correlated_at=None,
                    quarantined_at=None,
                    claim_token=None,
                    claimed_by=None,
                    claimed_at=None,
                    last_requeue_audit_id=None,
                    created_at=received_at,
                    updated_at=received_at,
                )
            )


def _insert_quarantined_early_inbox_batch(Session, reserved, count: int) -> None:
    with Session.begin() as session:
        for index in range(count):
            received_at = reserved.reserved_at + timedelta(microseconds=index)
            session.add(
                MetaCallbackInboxRow(
                    id=f"quarantined-batch-{uuid4().hex}",
                    provider_message_id=reserved.provider_message_id,
                    deduplication_key=f"{index:064x}",
                    provider_status="delivered",
                    provider_timestamp_raw=str(1_788_220_800 + index),
                    provider_timestamp=datetime.fromtimestamp(
                        1_788_220_800 + index, tz=UTC
                    ),
                    received_at=received_at,
                    valid=True,
                    errors_present=False,
                    error_fingerprint=None,
                    state="QUARANTINED",
                    reconciliation_id=None,
                    correlation_attempt_count=5,
                    next_correlation_at=received_at,
                    last_error_code=(
                        "PRODUCTION_META_RECONCILIATION_NOT_FOUND"
                    ),
                    correlated_at=None,
                    quarantined_at=received_at,
                    claim_token=None,
                    claimed_by=None,
                    claimed_at=None,
                    last_requeue_audit_id=None,
                    created_at=received_at,
                    updated_at=received_at,
                )
            )


def test_101_exact_quarantines_recover_in_bounded_sql_batches(Session):
    reserved = _reserved_attempt(Session, suffix=uuid4().hex)
    _insert_quarantined_early_inbox_batch(Session, reserved, 101)
    statements = 0
    engine = Session.kw["bind"]

    def count_sql(conn, cursor, statement, parameters, context, executemany):
        nonlocal statements
        statements += 1

    event.listen(engine, "before_cursor_execute", count_sql)
    try:
        with Session.begin() as session:
            mark_production_meta_api_accepted(
                session,
                outbox_message_id=reserved.outbox_id,
                dispatcher_id="v2-test-dispatcher",
                provider_message_id=reserved.provider_message_id,
                now=reserved.reserved_at + timedelta(seconds=1),
            )
    finally:
        event.remove(engine, "before_cursor_execute", count_sql)
    assert statements < 80
    with Session() as session:
        reconciliation_id = _accepted_reconciliation_id(
            session, reserved.outbox_id
        )
        assert session.scalar(
            select(func.count()).select_from(MetaCallbackInboxRow).where(
                MetaCallbackInboxRow.provider_message_id
                == reserved.provider_message_id,
                MetaCallbackInboxRow.state == "QUARANTINED",
            )
        ) == 1
    assert reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=reconciliation_id,
        now=reserved.reserved_at + timedelta(seconds=2),
    ).decision == "PASSED"
    with Session() as session:
        assert session.scalar(
            select(func.count()).select_from(MetaCallbackEvidenceRow).where(
                MetaCallbackEvidenceRow.reconciliation_id == reconciliation_id
            )
        ) == 101
        assert session.scalar(
            select(func.count()).select_from(AuditEventRow).where(
                AuditEventRow.event_type
                == "production_meta_callback_exact_acceptance_requeued",
                AuditEventRow.payload["reconciliation_id"].as_string()
                == reconciliation_id,
            )
        ) == 101


def test_backlog_is_fully_drained_before_failed_terminal_reduction(Session):
    reserved = _reserved_attempt(Session, suffix=uuid4().hex)
    _insert_pending_inbox_batch(
        Session,
        reserved,
        ["failed"] * 200 + ["delivered"],
    )
    with Session.begin() as session:
        mark_production_meta_api_accepted(
            session,
            outbox_message_id=reserved.outbox_id,
            dispatcher_id="v2-test-dispatcher",
            provider_message_id=reserved.provider_message_id,
            now=reserved.reserved_at + timedelta(seconds=1),
        )
    with Session() as session:
        reconciliation_id = _accepted_reconciliation_id(
            session, reserved.outbox_id
        )
        assert session.get(
            MetaDeliveryReconciliationRow, reconciliation_id
        ).state == "PENDING"

    first_closure = reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=reconciliation_id,
        now=reserved.reserved_at + timedelta(days=8),
    )
    assert first_closure.decision == "PENDING"
    assert first_closure.evidence_class == "INBOX_BACKLOG"
    second_closure = reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=reconciliation_id,
        now=reserved.reserved_at + timedelta(days=8),
    )
    assert second_closure.decision == "PASSED"
    with Session() as session:
        assert session.scalar(
            select(func.count()).select_from(MetaCallbackEvidenceRow).where(
                MetaCallbackEvidenceRow.reconciliation_id == reconciliation_id
            )
        ) == 201


def test_crashed_inbox_claim_is_recoverable_only_at_lease_expiry(Session):
    module = __import__(
        "attention_router.platform.meta_callback_reconciliation",
        fromlist=["_claim_pending_meta_callback_inbox"],
    )
    provider_id = "claim-crash-" + uuid4().hex
    admission = admit_meta_callback_evidence(
        Session,
        status_event=_callback(provider_id),
        received_at=datetime.now(UTC),
    )
    _park_other_pending_inbox(Session, keep_id=admission.inbox_id)
    claimed_at = datetime.now(UTC)
    claim = module._claim_pending_meta_callback_inbox(
        Session,
        worker_id="crashed-worker",
        now=claimed_at,
        excluded=set(),
        transaction_timeout_seconds=10,
    )
    assert claim is not None
    before_expiry = reconcile_pending_meta_callback_inbox(
        Session,
        worker_id="replacement-worker",
        now=claimed_at + timedelta(seconds=29),
        limit=1,
    )
    at_expiry = reconcile_pending_meta_callback_inbox(
        Session,
        worker_id="replacement-worker",
        now=claimed_at + timedelta(seconds=30),
        limit=1,
    )
    assert before_expiry.selected == 0
    assert at_expiry.selected == 1
    with Session() as session:
        inbox = session.get(MetaCallbackInboxRow, admission.inbox_id)
        assert inbox.correlation_attempt_count == 1
        assert inbox.claim_token is None
        inbox.next_correlation_at = datetime(2100, 1, 1, tzinfo=UTC)
        session.commit()


def test_legacy_evidence_exact_replay_converges_without_insert_or_hot_loop(Session):
    module = __import__(
        "attention_router.platform.meta_callback_reconciliation",
        fromlist=["_evidence_deduplication_key"],
    )
    attempt = _accepted_attempt(Session, suffix=uuid4().hex)
    received_at = attempt.accepted_at + timedelta(seconds=1)
    deduplication_key = module._evidence_deduplication_key(
        provider_status="delivered",
        provider_timestamp_raw=1_788_220_800,
        error_fingerprint=None,
    )
    with Session() as session:
        session.add(
            MetaCallbackEvidenceRow(
                id="legacy-evidence-" + uuid4().hex,
                reconciliation_id=attempt.reconciliation_id,
                inbox_id=None,
                deduplication_key=deduplication_key,
                provider_status="delivered",
                provider_timestamp_raw="1788220800",
                provider_timestamp=datetime.fromtimestamp(1_788_220_800, tz=UTC),
                received_at=received_at,
                valid=True,
                admissible=True,
                errors_present=False,
                error_fingerprint=None,
                created_at=received_at,
            )
        )
        session.commit()
    admission = admit_meta_callback_evidence(
        Session,
        status_event=_callback(attempt.provider_message_id),
        received_at=received_at + timedelta(seconds=1),
    )
    assert admission.evidence_persisted is True
    _park_other_pending_inbox(Session)
    with Session() as session:
        inbox = session.get(MetaCallbackInboxRow, admission.inbox_id)
        assert inbox.state == "CORRELATED"
        assert session.scalar(
            select(func.count()).select_from(MetaCallbackEvidenceRow).where(
                MetaCallbackEvidenceRow.reconciliation_id == attempt.reconciliation_id
            )
        ) == 1
    for _ in range(5):
        assert reconcile_pending_meta_callback_inbox(
            Session,
            worker_id="legacy-evidence-worker",
            now=received_at + timedelta(days=1),
            limit=100,
        ).selected == 0


def test_reconciliation_quarantine_requires_scoped_audited_requeue(Session):
    attempt = _accepted_attempt(Session, suffix=uuid4().hex)
    timestamp = _quarantine_reconciliation(Session, attempt)
    with pytest.raises(DBAPIError):
        with Session.begin() as session:
            session.execute(
                update(MetaDeliveryReconciliationRow)
                .where(
                    MetaDeliveryReconciliationRow.id
                    == attempt.reconciliation_id
                )
                .values(
                    operational_state="ACTIVE",
                    failure_count=0,
                    last_failure_at=None,
                    last_failure_code=None,
                    quarantined_at=None,
                    quarantine_reason=None,
                )
            )
    admit_meta_callback_evidence(
        Session,
        status_event=_callback(attempt.provider_message_id),
        received_at=attempt.deadline_at,
    )
    with pytest.raises(SafetyDenied, match="RECONCILIATION_QUARANTINED"):
        reconcile_meta_callback_outcome(
            Session,
            reconciliation_id=attempt.reconciliation_id,
            now=attempt.deadline_at,
        )
    with Session() as session:
        with pytest.raises(ValueError, match="operator"):
            recover_quarantined_meta_reconciliation(
                session,
                reconciliation_id=attempt.reconciliation_id,
                operator_id="",
                reason="REVIEWED_SAFE_RETRY",
                now=timestamp,
            )
        with pytest.raises(ValueError, match="reason"):
            recover_quarantined_meta_reconciliation(
                session,
                reconciliation_id=attempt.reconciliation_id,
                operator_id="release-operator",
                reason="",
                now=timestamp,
            )
        assert recover_quarantined_meta_reconciliation(
            session,
            reconciliation_id=attempt.reconciliation_id,
            operator_id="release-operator",
            reason="REVIEWED_SAFE_RETRY",
            now=timestamp,
        )
        assert not recover_quarantined_meta_reconciliation(
            session,
            reconciliation_id=attempt.reconciliation_id,
            operator_id="release-operator",
            reason="REVIEWED_SAFE_RETRY",
            now=timestamp,
        )
        session.commit()
    assert reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.deadline_at,
    ).decision == "PASSED"
    with Session() as session:
        audits = session.scalars(
            select(AuditEventRow).where(
                AuditEventRow.event_type
                == "production_meta_reconciliation_requeued",
                AuditEventRow.payload["reconciliation_id"].as_string()
                == attempt.reconciliation_id,
            )
        ).all()
        assert len(audits) == 1
        reconciliation = session.get(
            MetaDeliveryReconciliationRow, attempt.reconciliation_id
        )
        assert reconciliation.last_requeue_audit_id == audits[0].id
        assert audits[0].payload["operator_fingerprint"]
        assert audits[0].payload["reason_code"] == "REVIEWED_SAFE_RETRY"
        assert "operator_id" not in audits[0].payload


def test_generic_inbox_quarantine_blocks_terminal_until_audited_requeue(
    Session, monkeypatch
):
    attempt = _accepted_attempt(Session, suffix=uuid4().hex)
    module = __import__(
        "attention_router.platform.meta_callback_reconciliation",
        fromlist=[
            "_correlate_provider_inbox_in_transaction",
            "acquire_meta_provider_gate",
        ],
    )

    original_correlation = module._correlate_provider_inbox_in_transaction

    def defer_initial_correlation(*args, **kwargs):
        raise RuntimeError("synthetic-initial-deferral")

    monkeypatch.setattr(
        module,
        "_correlate_provider_inbox_in_transaction",
        defer_initial_correlation,
    )
    admission = admit_meta_callback_evidence(
        Session,
        status_event=_callback(attempt.provider_message_id),
        received_at=attempt.accepted_at,
    )
    monkeypatch.setattr(
        module,
        "_correlate_provider_inbox_in_transaction",
        original_correlation,
    )
    _park_other_pending_inbox(Session, keep_id=admission.inbox_id)
    original_gate = module.acquire_meta_provider_gate

    def processing_failure(session, provider_message_id):
        raise RuntimeError("synthetic-processing-failure")

    monkeypatch.setattr(module, "acquire_meta_provider_gate", processing_failure)
    with Session() as session:
        current = session.get(
            MetaCallbackInboxRow, admission.inbox_id
        ).next_correlation_at
    for _ in range(5):
        reconcile_pending_meta_callback_inbox(
            Session,
            worker_id="generic-failure-worker",
            now=current,
            limit=1,
        )
        with Session() as session:
            inbox = session.get(MetaCallbackInboxRow, admission.inbox_id)
            current = inbox.next_correlation_at
    monkeypatch.setattr(module, "acquire_meta_provider_gate", original_gate)

    with Session() as session:
        inbox = session.get(MetaCallbackInboxRow, admission.inbox_id)
        assert inbox.state == "QUARANTINED"
        assert inbox.last_error_code == "RuntimeError"
    with pytest.raises(
        SafetyDenied, match="PRODUCTION_META_CALLBACK_INBOX_QUARANTINED"
    ):
        reconcile_meta_callback_outcome(
            Session,
            reconciliation_id=attempt.reconciliation_id,
            now=attempt.closure_after,
        )
    with pytest.raises(DBAPIError):
        with Session.begin() as session:
            session.execute(
                update(MetaCallbackInboxRow)
                .where(MetaCallbackInboxRow.id == admission.inbox_id)
                .values(
                    state="PENDING",
                    correlation_attempt_count=0,
                    next_correlation_at=current,
                    last_error_code=None,
                    quarantined_at=None,
                )
            )
    with Session() as session:
        with pytest.raises(ValueError, match="approved requeue reason"):
            recover_quarantined_meta_callback_inbox(
                session,
                inbox_id=admission.inbox_id,
                operator_id="release-operator",
                reason=attempt.provider_message_id,
                now=current,
            )
    with Session.begin() as session:
        assert recover_quarantined_meta_callback_inbox(
            session,
            inbox_id=admission.inbox_id,
            operator_id="release-operator",
            reason="REVIEWED_TRANSIENT_FAILURE",
            now=current,
        )
        assert not recover_quarantined_meta_callback_inbox(
            session,
            inbox_id=admission.inbox_id,
            operator_id="release-operator",
            reason="REVIEWED_TRANSIENT_FAILURE",
            now=current,
        )
    sweep = reconcile_pending_meta_callback_inbox(
        Session,
        worker_id="generic-retry-worker",
        now=current,
        limit=1,
    )
    assert sweep.correlated == 1
    assert reconcile_meta_callback_outcome(
        Session,
        reconciliation_id=attempt.reconciliation_id,
        now=attempt.closure_after,
    ).decision == "PASSED"
    with Session() as session:
        inbox = session.get(MetaCallbackInboxRow, admission.inbox_id)
        assert inbox.state == "CORRELATED"
        assert inbox.last_requeue_audit_id is not None
        assert session.scalar(
            select(func.count()).select_from(AuditEventRow).where(
                AuditEventRow.event_type
                == "production_meta_callback_inbox_requeued",
                AuditEventRow.payload["inbox_id"].as_string()
                == admission.inbox_id,
            )
        ) == 1


def _pending_authorization(Session):
    now = datetime.now(UTC)
    intent_id = "failed-handler-intent-" + uuid4().hex
    scope = {
        "authority": "frozen",
        "target": "redacted-test-target",
        "test_identity": intent_id,
    }
    with Session() as session:
        intent = ExecutionIntentRow(
            id=intent_id,
            idempotency_key=intent_id,
            scope=scope,
            scope_fingerprint=fingerprint(scope),
            provenance={"source": "release-gate-remediation-test"},
            state="FROZEN",
            created_at=now,
            frozen_at=now,
        )
        session.add(intent)
        session.flush()
        authorization = prepare(
            session,
            execution_intent_id=intent.id,
            expected_approver="redacted-approver",
            ttl_seconds=300,
            correlation_id="failed-handler-correlation-" + uuid4().hex,
            now=now,
        )
        provider_id = "failed-handler-provider-" + uuid4().hex
        request_approval(session, authorization.id, provider_id)
        session.commit()
        return (
            intent.id,
            authorization.id,
            authorization.execution_intent_fingerprint,
            provider_id,
            authorization.expires_at,
        )


def test_signed_failed_handler_closes_hea_from_normalized_inbox(
    Session, signed_client
):
    intent_id, authorization_id, intent_fingerprint, provider_id, expires_at = (
        _pending_authorization(Session)
    )
    payload = meta_status(provider_id)
    payload["entry"][0]["changes"][0]["value"]["statuses"][0]["status"] = (
        "failed"
    )
    response = post_signed(signed_client, payload)
    assert response.status_code == 200
    with Session() as session:
        close_failed_production_human_approval(
            session,
            authorization_id=authorization_id,
            expected_request_wamid=provider_id,
            expected_execution_intent_fingerprint=intent_fingerprint,
            now=expires_at + timedelta(seconds=31),
        )
        session.commit()
    with Session() as session:
        assert session.get(HumanExecutionAuthorizationRow, authorization_id).state == (
            "REVOKED"
        )
        assert session.get(ExecutionIntentRow, intent_id).state == "RETIRED"
        closure = session.scalar(
            select(AuditEventRow).where(
                AuditEventRow.event_type
                == "human_execution_authorization_revoked",
                AuditEventRow.payload["authorization_id"].as_string()
                == authorization_id,
            )
        )
        assert closure.payload["meta_status_evidence_id"]
        assert closure.payload["request_wamid_hash"]
        assert provider_id not in str(closure.payload)


def test_delivered_admission_commit_serializes_before_failed_hea_closure(Session):
    intent_id, authorization_id, intent_fingerprint, provider_id, expires_at = (
        _pending_authorization(Session)
    )
    failed_received_at = expires_at - timedelta(seconds=10)
    admit_meta_callback_evidence(
        Session,
        status_event=_callback(provider_id, status="failed"),
        received_at=failed_received_at,
    )

    engine = Session.kw["bind"]
    delivered_inserted = Event()
    release_delivery_commit = Event()
    close_waiting_for_gate = Event()

    def pause_delivered_insert(
        conn, cursor, statement, parameters, context, executemany
    ):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("insert into meta_callback_inbox"):
            delivered_inserted.set()
            assert release_delivery_commit.wait(timeout=10)

    def observe_close_gate(
        conn, cursor, statement, parameters, context, executemany
    ):
        if "pg_advisory_xact_lock" in statement.lower():
            close_waiting_for_gate.set()

    event.listen(engine, "after_cursor_execute", pause_delivered_insert)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            delivery = pool.submit(
                admit_meta_callback_evidence,
                Session,
                status_event={
                    **_callback(provider_id, status="delivered"),
                    "timestamp": 1_788_220_801,
                },
                received_at=expires_at - timedelta(seconds=1),
            )
            assert delivered_inserted.wait(timeout=10)

            event.listen(engine, "before_cursor_execute", observe_close_gate)

            def close_failure():
                try:
                    with Session.begin() as session:
                        close_failed_production_human_approval(
                            session,
                            authorization_id=authorization_id,
                            expected_request_wamid=provider_id,
                            expected_execution_intent_fingerprint=(
                                intent_fingerprint
                            ),
                            now=expires_at + timedelta(seconds=31),
                        )
                except PermissionError as exc:
                    return str(exc)
                return "CLOSED"

            closure = pool.submit(close_failure)
            assert close_waiting_for_gate.wait(timeout=10)
            release_delivery_commit.set()
            assert delivery.result(timeout=20).evidence_persisted is True
            assert closure.result(timeout=20) == "META_DELIVERY_FAILURE_NOT_OBSERVED"
            event.remove(engine, "before_cursor_execute", observe_close_gate)
    finally:
        release_delivery_commit.set()
        if event.contains(engine, "after_cursor_execute", pause_delivered_insert):
            event.remove(engine, "after_cursor_execute", pause_delivered_insert)
        if event.contains(engine, "before_cursor_execute", observe_close_gate):
            event.remove(engine, "before_cursor_execute", observe_close_gate)
    with Session() as session:
        assert session.get(HumanExecutionAuthorizationRow, authorization_id).state == (
            "PENDING_HUMAN_APPROVAL"
        )
        assert session.get(ExecutionIntentRow, intent_id).state == "FROZEN"


def test_dbapi_provider_parameter_is_absent_from_logs(Session, caplog, monkeypatch):
    provider_id = "redaction-canary-" + uuid4().hex
    module = __import__(
        "attention_router.platform.meta_callback_reconciliation",
        fromlist=["_correlate_provider_inbox_in_transaction"],
    )

    def fail_with_parameter(session, *, provider_message_id, now, inbox_id=None):
        session.execute(
            text("select cast(:provider_message_id as integer)"),
            {"provider_message_id": provider_message_id},
        )

    monkeypatch.setattr(
        module, "_correlate_provider_inbox_in_transaction", fail_with_parameter
    )
    caplog.set_level(logging.ERROR)
    result = admit_meta_callback_evidence(
        Session,
        status_event=_callback(provider_id, status="failed"),
        received_at=datetime.now(UTC),
    )
    assert result.evidence_persisted is True
    assert provider_id not in caplog.text
    assert "[parameters:" not in caplog.text
    assert "Traceback" not in caplog.text
