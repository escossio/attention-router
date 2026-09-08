from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from attention_router.infrastructure.models import ExecutionIntentRow, HumanExecutionAuthorizationRow
from attention_router.platform.human_execution_authorization import fingerprint, prepare, request_approval

pytestmark = pytest.mark.postgres


def _intent(session, value):
    now = datetime.now(UTC)
    row = ExecutionIntentRow(
        id=str(uuid4()), idempotency_key=str(uuid4()), scope={"case": value},
        scope_fingerprint=fingerprint({"case": value}), provenance={"source": "test"},
        state="FROZEN", created_at=now, frozen_at=now,
    )
    session.add(row)
    session.flush()
    return row


def test_two_new_heas_for_distinct_execution_intents_coexist(Session):
    with Session() as session:
        first = _intent(session, "first")
        second = _intent(session, "second")
        first_auth = prepare(session, execution_intent_id=first.id, expected_approver="owner", ttl_seconds=300, correlation_id=str(uuid4()))
        second_auth = prepare(session, execution_intent_id=second.id, expected_approver="owner", ttl_seconds=300, correlation_id=str(uuid4()))
        request_approval(session, first_auth.id, "wamid.first")
        request_approval(session, second_auth.id, "wamid.second")
        session.commit()
        created_ids = {first_auth.id, second_auth.id}
        assert session.scalar(select(func.count()).select_from(HumanExecutionAuthorizationRow).where(HumanExecutionAuthorizationRow.id.in_(created_ids))) == 2
        assert first_auth.execution_intent_id == first.id
        assert second_auth.execution_intent_id == second.id
        assert first_auth.scope_fingerprint is None
        assert second_auth.scope_fingerprint is None


def test_same_execution_intent_can_have_multiple_pending_authorizations(Session):
    with Session() as session:
        intent = _intent(session, "reissue")
        first = prepare(session, execution_intent_id=intent.id, expected_approver="owner", ttl_seconds=300, correlation_id=str(uuid4()))
        second = prepare(session, execution_intent_id=intent.id, expected_approver="owner", ttl_seconds=300, correlation_id=str(uuid4()))
        request_approval(session, first.id, "wamid.reissue.first")
        request_approval(session, second.id, "wamid.reissue.second")
        session.commit()
        rows = session.scalars(select(HumanExecutionAuthorizationRow).where(HumanExecutionAuthorizationRow.execution_intent_id == intent.id)).all()
        assert {row.id for row in rows} == {first.id, second.id}
        assert all(row.state == "PENDING_HUMAN_APPROVAL" for row in rows)
        assert all(row.scope_fingerprint is None for row in rows)
