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
    HumanExecutionAuthorizationRow,
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
    admit_meta_callback_evidence,
)
from attention_router.platform.production_authority import ProductionAuthorityDenied
from attention_router.platform.production_bridge import materialize_production_agent_intent


pytestmark = pytest.mark.postgres


def test_postgres_failed_delivery_closure_is_atomic_inert_and_idempotent(Session):
    now = datetime.now(UTC)
    scope = {"authority": "frozen", "target": "bounded-test-target"}
    intent_id = "failed-close-" + uuid4().hex
    request_wamid = "wamid.failed-close." + uuid4().hex
    graph_models = (
        AgentExecutionIntentRow,
        ScenarioRunRow,
        EffectBudgetRow,
        ExecutionLeaseRow,
        BoundedRunAuthorizationRow,
        OutboxMessageRow,
    )
    with Session() as session:
        graph_counts_before = {
            model: session.scalar(select(func.count()).select_from(model))
            for model in graph_models
        }

    with Session() as session:
        intent = ExecutionIntentRow(
            id=intent_id,
            idempotency_key=intent_id,
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
        request_approval(session, hea.id, request_wamid)
        session.commit()
        hea_id = hea.id
        intent_fingerprint = intent.scope_fingerprint

    admission = admit_meta_callback_evidence(
        Session,
        status_event={
            "id": request_wamid,
            "status": "failed",
            "timestamp": 1_788_220_800,
            "errors": [],
        },
        received_at=now,
    )
    assert admission.evidence_persisted is True

    with Session.begin() as session:
        first = close_failed_production_human_approval(
            session,
            authorization_id=hea_id,
            expected_request_wamid=request_wamid,
            expected_execution_intent_fingerprint=intent_fingerprint,
            now=now + timedelta(seconds=331),
        )
        second = close_failed_production_human_approval(
            session,
            authorization_id=hea_id,
            expected_request_wamid=request_wamid,
            expected_execution_intent_fingerprint=intent_fingerprint,
            now=now + timedelta(seconds=331),
        )
        assert first[0].id == second[0].id
        assert first[1].id == second[1].id

    with Session() as session:
        hea = session.get(HumanExecutionAuthorizationRow, hea_id)
        intent = session.get(ExecutionIntentRow, intent_id)
        assert hea.state == "REVOKED"
        assert intent.state == "RETIRED"
        assert intent.scope == scope
        assert intent.scope_fingerprint == intent_fingerprint
        assert intent.retired_at is not None
        assert session.scalar(
            select(func.count())
            .select_from(AuditEventRow)
            .where(
                AuditEventRow.event_type
                == "human_execution_authorization_revoked",
                AuditEventRow.payload["authorization_id"].as_string() == hea_id,
            )
        ) == 1
        with pytest.raises(ProductionAuthorityDenied, match="EXECUTION_INTENT_NOT_FROZEN"):
            materialize_production_agent_intent(
                session,
                execution_intent_id=intent_id,
                expected_fingerprint=intent_fingerprint,
                agent_decision_id="must-not-materialize",
                effective_response_snapshot="must-not-materialize",
            )
        for model in graph_models:
            assert (
                session.scalar(select(func.count()).select_from(model))
                == graph_counts_before[model]
            )
