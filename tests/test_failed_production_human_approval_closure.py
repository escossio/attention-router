from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from attention_router.infrastructure.models import (
    AgentExecutionIntentRow,
    AuditEventRow,
    BoundedRunAuthorizationRow,
    EffectBudgetRow,
    ExecutionIntentRow,
    ExecutionLeaseRow,
    MetaCallbackInboxRow,
    OutboxMessageRow,
    ScenarioRunRow,
)
from attention_router.platform.human_approval_integration import (
    close_failed_production_human_approval,
)
from attention_router.platform.human_execution_authorization import (
    fingerprint,
    prepare,
    request_approval,
)
from attention_router.platform.meta_callback_reconciliation import (
    persist_human_approval_delivery_evidence,
)
from attention_router.platform.production_authority import ProductionAuthorityDenied
from attention_router.platform.production_bridge import materialize_production_agent_intent


def _pending_authorization(session):
    now = datetime.now(UTC)
    scope = {"authority": "frozen", "target": "bounded-test-target"}
    intent = ExecutionIntentRow(
        id="intent-" + uuid4().hex,
        idempotency_key="intent-key-" + uuid4().hex,
        scope=scope,
        scope_fingerprint=fingerprint(scope),
        provenance={"source": "failed-delivery-closure-test"},
        state="FROZEN",
        created_at=now,
        frozen_at=now,
    )
    session.add(intent)
    session.flush()
    hea = prepare(
        session,
        execution_intent_id=intent.id,
        expected_approver="approver",
        ttl_seconds=300,
        correlation_id="corr-" + uuid4().hex,
        now=now,
    )
    request_wamid = "wamid.request." + uuid4().hex
    request_approval(session, hea.id, request_wamid)
    return intent, hea, request_wamid


def _observe_failed_status(session, request_wamid):
    timestamp = datetime.now(UTC)
    inbox = MetaCallbackInboxRow(
        id="failed-inbox-" + uuid4().hex,
        provider_message_id=request_wamid,
        deduplication_key="failed-dedup-" + uuid4().hex,
        provider_status="failed",
        provider_timestamp_raw="1788220800",
        provider_timestamp=timestamp,
        received_at=timestamp,
        valid=True,
        errors_present=True,
        error_fingerprint="failed-error-" + uuid4().hex,
        state="PENDING",
        reconciliation_id=None,
        correlation_attempt_count=0,
        next_correlation_at=timestamp,
        last_error_code=None,
        correlated_at=None,
        quarantined_at=None,
        claim_token=None,
        claimed_by=None,
        claimed_at=None,
        created_at=timestamp,
        updated_at=timestamp,
    )
    session.add(inbox)
    session.flush()
    persist_human_approval_delivery_evidence(
        session,
        inbox=inbox,
    )


def _closure_time(hea):
    return hea.expires_at + timedelta(seconds=31)


def test_failed_delivery_closure_revokes_hea_and_retires_exact_intent(session):
    intent, hea, request_wamid = _pending_authorization(session)
    original_fingerprint = intent.scope_fingerprint
    _observe_failed_status(session, request_wamid)

    closed_hea, retired = close_failed_production_human_approval(
        session,
        authorization_id=hea.id,
        expected_request_wamid=request_wamid,
        expected_execution_intent_fingerprint=hea.execution_intent_fingerprint,
        now=_closure_time(hea),
    )
    session.commit()

    assert closed_hea.state == "REVOKED"
    assert retired.state == "RETIRED"
    assert retired.scope_fingerprint == original_fingerprint
    assert retired.retired_at is not None
    with pytest.raises(ProductionAuthorityDenied, match="EXECUTION_INTENT_NOT_FROZEN"):
        materialize_production_agent_intent(
            session,
            execution_intent_id=retired.id,
            expected_fingerprint=original_fingerprint,
            agent_decision_id="must-not-materialize",
            effective_response_snapshot="must-not-materialize",
        )
    closure = session.scalar(
        select(AuditEventRow).where(
            AuditEventRow.event_type
            == "human_execution_authorization_revoked"
        )
    )
    assert closure.payload["reason_code"] == "META_DELIVERY_FAILED"
    assert closure.payload["meta_status_evidence_id"]
    assert "request_wamid" not in closure.payload
    assert closure.payload["request_wamid_hash"]
    assert closure.previous_state == "PENDING_HUMAN_APPROVAL"
    assert closure.next_state == "REVOKED"


def test_failed_delivery_closure_requires_exact_persisted_failure(session):
    intent, hea, request_wamid = _pending_authorization(session)

    with pytest.raises(PermissionError, match="META_DELIVERY_FAILURE_NOT_OBSERVED"):
        close_failed_production_human_approval(
            session,
            authorization_id=hea.id,
            expected_request_wamid=request_wamid,
            expected_execution_intent_fingerprint=hea.execution_intent_fingerprint,
        )

    assert hea.state == "PENDING_HUMAN_APPROVAL"
    assert intent.state == "FROZEN"


def test_failed_delivery_closure_waits_for_admission_grace(session):
    intent, hea, request_wamid = _pending_authorization(session)
    _observe_failed_status(session, request_wamid)

    with pytest.raises(PermissionError, match="META_DELIVERY_FAILURE_NOT_STABLE"):
        close_failed_production_human_approval(
            session,
            authorization_id=hea.id,
            expected_request_wamid=request_wamid,
            expected_execution_intent_fingerprint=hea.execution_intent_fingerprint,
            now=hea.expires_at + timedelta(seconds=29),
        )

    assert hea.state == "PENDING_HUMAN_APPROVAL"
    assert intent.state == "FROZEN"


def test_failed_delivery_closure_rejects_wrong_request_wamid(session):
    intent, hea, request_wamid = _pending_authorization(session)
    _observe_failed_status(session, request_wamid)

    with pytest.raises(PermissionError, match="HUMAN_AUTH_REQUEST_WAMID_MISMATCH"):
        close_failed_production_human_approval(
            session,
            authorization_id=hea.id,
            expected_request_wamid="wamid.other",
            expected_execution_intent_fingerprint=hea.execution_intent_fingerprint,
        )

    assert hea.state == "PENDING_HUMAN_APPROVAL"
    assert intent.state == "FROZEN"


def test_failed_delivery_closure_rejects_wrong_fingerprint(session):
    intent, hea, request_wamid = _pending_authorization(session)
    _observe_failed_status(session, request_wamid)

    with pytest.raises(PermissionError, match="EXECUTION_INTENT_FINGERPRINT_MISMATCH"):
        close_failed_production_human_approval(
            session,
            authorization_id=hea.id,
            expected_request_wamid=request_wamid,
            expected_execution_intent_fingerprint="wrong-fingerprint",
        )

    assert hea.state == "PENDING_HUMAN_APPROVAL"
    assert intent.state == "FROZEN"


def test_failed_delivery_closure_is_idempotent_and_creates_no_execution(session):
    _, hea, request_wamid = _pending_authorization(session)
    _observe_failed_status(session, request_wamid)

    first = close_failed_production_human_approval(
        session,
        authorization_id=hea.id,
        expected_request_wamid=request_wamid,
        expected_execution_intent_fingerprint=hea.execution_intent_fingerprint,
        now=_closure_time(hea),
    )
    second = close_failed_production_human_approval(
        session,
        authorization_id=hea.id,
        expected_request_wamid=request_wamid,
        expected_execution_intent_fingerprint=hea.execution_intent_fingerprint,
        now=_closure_time(hea),
    )
    session.commit()

    assert first[0].id == second[0].id
    assert first[1].id == second[1].id
    assert session.scalar(
        select(func.count())
        .select_from(AuditEventRow)
        .where(
            AuditEventRow.event_type
            == "human_execution_authorization_revoked"
        )
    ) == 1
    for model in (
        AgentExecutionIntentRow,
        ScenarioRunRow,
        EffectBudgetRow,
        ExecutionLeaseRow,
        BoundedRunAuthorizationRow,
        OutboxMessageRow,
    ):
        assert session.scalar(select(func.count()).select_from(model)) == 0


def test_failed_delivery_closure_does_not_revoke_when_retirement_fails(session):
    intent, hea, request_wamid = _pending_authorization(session)
    _observe_failed_status(session, request_wamid)
    intent.state = "MATERIALIZED"
    session.flush()

    with pytest.raises(ProductionAuthorityDenied, match="EXECUTION_INTENT_NOT_RETIRABLE"):
        close_failed_production_human_approval(
            session,
            authorization_id=hea.id,
            expected_request_wamid=request_wamid,
            expected_execution_intent_fingerprint=hea.execution_intent_fingerprint,
            now=_closure_time(hea),
        )

    assert hea.state == "PENDING_HUMAN_APPROVAL"
    assert intent.state == "MATERIALIZED"


def test_failed_delivery_closure_rejects_non_pending_human_decision(session):
    intent, hea, request_wamid = _pending_authorization(session)
    _observe_failed_status(session, request_wamid)
    hea.state = "APPROVED"
    session.flush()

    with pytest.raises(PermissionError, match="HUMAN_AUTH_NOT_PENDING"):
        close_failed_production_human_approval(
            session,
            authorization_id=hea.id,
            expected_request_wamid=request_wamid,
            expected_execution_intent_fingerprint=hea.execution_intent_fingerprint,
            now=_closure_time(hea),
        )

    assert intent.state == "FROZEN"
