import pytest
from datetime import UTC, datetime
from uuid import uuid4
from attention_router.platform.human_execution_authorization import decide, fingerprint, prepare, request_approval
from attention_router.infrastructure.models import ExecutionIntentRow

def intent_id(session, value):
    ident = str(uuid4())
    session.add(ExecutionIntentRow(id=ident, idempotency_key=ident, scope=value, scope_fingerprint=fingerprint(value), provenance={}, state="FROZEN", created_at=datetime.now(UTC), frozen_at=datetime.now(UTC)))
    session.flush()
    return ident

def scope():
    return {"run_id":"run-1","scenario_version":"sv-1","safety_set":"safe-1","execution_class":"L1_REAL","target":"actor-1","transport":"meta_whatsapp_cloud","buttons":{"opaque-approve":"APPROVE","opaque-deny":"DENY"}}

def test_human_authorization_is_separate_scoped_and_single_decision(session):
    value = scope()
    row = prepare(session,execution_intent_id=intent_id(session, value),scope=value,expected_approver="actor-owner-1",ttl_seconds=300,correlation_id="corr")
    assert row.execution_intent_fingerprint == fingerprint(scope())
    request_approval(session,row.id,"wamid.request")
    assert decide(session,authorization_id=row.id,inbound_wamid="wamid.click",sender="actor-owner-1",context_id="wamid.request",button_id="opaque-approve")=="APPROVE"
    session.commit()
    with pytest.raises(PermissionError):
        decide(session,authorization_id=row.id,inbound_wamid="wamid.other",sender="actor-owner-1",context_id="wamid.request",button_id="opaque-deny")

def test_human_authorization_rejects_wrong_identity_context_and_button(session):
    value = scope()
    row = prepare(session,execution_intent_id=intent_id(session, value),scope=value,expected_approver="owner",ttl_seconds=300,correlation_id="corr")
    request_approval(session,row.id,"wamid.request")
    with pytest.raises(PermissionError):
        decide(session,authorization_id=row.id,inbound_wamid="click",sender="other",context_id="wamid.request",button_id="opaque-approve")
    with pytest.raises(PermissionError):
        decide(session,authorization_id=row.id,inbound_wamid="click",sender="owner",context_id="wrong",button_id="opaque-approve")
    with pytest.raises(PermissionError):
        decide(session,authorization_id=row.id,inbound_wamid="click",sender="owner",context_id="wamid.request",button_id="unknown")

def test_scope_changes_fingerprint():
    assert fingerprint(scope()) != fingerprint({**scope(), "target":"other"})
