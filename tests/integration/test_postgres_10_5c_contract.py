from datetime import UTC, datetime
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from attention_router.infrastructure.models import (
    BoundedRunAuthorizationRow,
    ExecutionIntentRow,
    HumanExecutionAuthorizationRow,
    OutboxMessageRow,
    ReadinessResultRow,
    ScenarioRunRow,
    TimerRow,
)
from attention_router.platform.human_authorization_control_gate import handle_meta_control_event
from attention_router.platform.human_execution_authorization import decide, fingerprint, prepare, request_approval

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def clean_contract_rows(Session):
    with Session() as session:
        session.execute(delete(HumanExecutionAuthorizationRow))
        session.execute(delete(ExecutionIntentRow))
        session.commit()


def _intent(session, scope=None, state="FROZEN"):
    scope = scope or {"scenario_version": "v1", "safety_set": {"name": "narrow", "version": 1},
                      "execution_class": "CONTROLLED", "target": {"audience": "test"},
                      "transport": {"channel": "meta"}, "budget_limits": {"max_effects": 1},
                      "buttons": {"approve": "APPROVE", "deny": "DENY"}}
    scope = dict(scope)
    scope["test_instance"] = str(uuid4())
    ident = str(uuid4())
    now = datetime.now(UTC)
    row = ExecutionIntentRow(id=ident, idempotency_key=ident, scope=scope,
                             scope_fingerprint=fingerprint(scope), provenance={"source": "test"},
                             state=state, created_at=now, frozen_at=now if state == "FROZEN" else None)
    session.add(row)
    session.flush()
    return row


def _auth(session, intent, approver="owner"):
    row = prepare(session, execution_intent_id=intent.id, scope=intent.scope,
                  expected_approver=approver, ttl_seconds=300, correlation_id=str(uuid4()))
    request_approval(session, row.id, "wamid.request")
    session.commit()
    return row


def test_intent_and_hea_binding_is_persisted(Session):
    with Session() as session:
        intent = _intent(session)
        auth = _auth(session, intent)
        stored = session.get(HumanExecutionAuthorizationRow, auth.id)
        assert stored.execution_intent_id == intent.id
        assert stored.execution_intent_fingerprint == intent.scope_fingerprint
        assert stored.state == "PENDING_HUMAN_APPROVAL"


def test_fingerprint_materiality_and_opaque_control_data():
    base = {"scenario_version": "v1", "safety_set": "safe", "execution_class": "L1",
            "target": "actor", "transport": "meta", "budget_limits": {"max": 1}}
    assert fingerprint(base) != fingerprint({**base, "scenario_version": "v2"})
    assert fingerprint(base) != fingerprint({**base, "target": "other"})
    assert fingerprint(base) == fingerprint(base)


@pytest.mark.parametrize("button,expected", [("approve", "APPROVE"), ("deny", "DENY")])
def test_decision_is_terminal_and_operationally_inert(Session, button, expected):
    with Session() as session:
        before = {name: session.scalar(select(func.count()).select_from(table)) for name, table in
                  [("runs", __import__("attention_router.infrastructure.models", fromlist=["ScenarioRunRow"]).ScenarioRunRow)]}
        intent = _intent(session)
        auth = _auth(session, intent)
        result = decide(session, authorization_id=auth.id, inbound_wamid=f"wamid.{button}",
                        sender="owner", context_id="wamid.request", button_id=button)
        session.commit()
        assert result == expected
        assert session.get(HumanExecutionAuthorizationRow, auth.id).state == ("APPROVED" if expected == "APPROVE" else "DENIED")
        after = session.scalar(select(func.count()).select_from(__import__("attention_router.infrastructure.models", fromlist=["ScenarioRunRow"]).ScenarioRunRow))
        assert after == before["runs"]


def test_decision_time_revalidation_and_replay(Session):
    with Session() as session:
        intent = _intent(session)
        auth = _auth(session, intent)
        intent.state = "RETIRED"
        session.commit()
        with pytest.raises(PermissionError):
            decide(session, authorization_id=auth.id, inbound_wamid="wamid.click", sender="owner", context_id="wamid.request", button_id="approve")
        assert session.get(HumanExecutionAuthorizationRow, auth.id).state == "PENDING_HUMAN_APPROVAL"


def test_gate_rejects_canary_and_disabled_control(Session):
    with Session() as session:
        intent = _intent(session)
        auth = _auth(session, intent)
        event = type("Event", (), {"external_event_id": "wamid.click", "actor_id": "owner",
            "correlation_id": "corr", "metadata": {"message_type": "interactive", "interactive_type": "button_reply",
            "button_reply_id": "interactive_canary_x_approve", "button_reply_title": "Autorizar",
            "context_id": "wamid.request", "sender": "owner"}})()
        assert handle_meta_control_event(session, event, enabled=True) == "CANARY_NOT_HUMAN_AUTH"
        assert handle_meta_control_event(session, event, enabled=False) == "GATE_DISABLED"
        assert session.get(HumanExecutionAuthorizationRow, auth.id).state == "PENDING_HUMAN_APPROVAL"


def test_approve_deny_race_has_one_terminal_winner(Session):
    with Session() as setup:
        intent = _intent(setup)
        auth = _auth(setup, intent)
        auth_id = auth.id
    barrier = Barrier(2)
    def attempt(button):
        with Session() as session:
            barrier.wait()
            try:
                result = decide(session, authorization_id=auth_id, inbound_wamid=f"wamid.{button}", sender="owner", context_id="wamid.request", button_id=button)
                session.commit()
                return result
            except (PermissionError, ValueError):
                return "REJECTED"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["approve", "deny"]))
    with Session() as session:
        row = session.get(HumanExecutionAuthorizationRow, auth_id)
        assert row.state in {"APPROVED", "DENIED"}
        winner_action = "APPROVE" if row.state == "APPROVED" else "DENY"
        assert results.count(winner_action) == 1
        assert sum(result in {"APPROVE", "DENY"} for result in results) == 1
        assert row.decision_at is not None
        assert sum(result == "REJECTED" for result in results) == 1
        assert session.scalar(select(func.count()).select_from(HumanExecutionAuthorizationRow).where(HumanExecutionAuthorizationRow.id == auth_id, HumanExecutionAuthorizationRow.state.in_(["APPROVED", "DENIED"]))) == 1
        for model in (ReadinessResultRow, BoundedRunAuthorizationRow, ScenarioRunRow, OutboxMessageRow, TimerRow):
            assert session.scalar(select(func.count()).select_from(model)) == 0
