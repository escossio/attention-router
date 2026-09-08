from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from attention_router.platform.human_authorization_control_gate import handle_meta_control_event
from attention_router.platform.human_execution_authorization import prepare, request_approval
from attention_router.infrastructure.models import ExecutionIntentRow
from attention_router.platform.human_execution_authorization import fingerprint
from uuid import uuid4

def intent_id(session, value):
    ident = str(uuid4())
    now = datetime.now(UTC)
    session.add(ExecutionIntentRow(id=ident, idempotency_key=ident, scope=value, scope_fingerprint=fingerprint(value), provenance={}, state="FROZEN", created_at=now, frozen_at=now))
    session.flush()
    return ident

def event(button, sender="owner", context="wamid.request", event_id="wamid.click"):
    return SimpleNamespace(external_event_id=event_id, actor_id=sender, correlation_id="corr", metadata={"message_type":"interactive","interactive_type":"button_reply","button_reply_id":button,"button_reply_title":"Autorizar","context_id":context,"sender":sender})

def test_gate_approves_only_correlated_human_authorization(session):
    scope={"run_id":"run","scenario_version":"sv","safety_set":"safe","execution_class":"L1","target":"target","transport":"meta_whatsapp_cloud","buttons":{"approve":"APPROVE","deny":"DENY"}}
    row=prepare(session,execution_intent_id=intent_id(session, scope),scope=scope,expected_approver="owner",ttl_seconds=300,correlation_id="corr")
    request_approval(session,row.id,"wamid.request")
    assert handle_meta_control_event(session,event("approve"),enabled=True)=="APPROVE"
    assert row.state=="APPROVED"
    assert handle_meta_control_event(session,event("approve",event_id="wamid.click.2"),enabled=True)=="DUPLICATE"

def test_gate_rejects_canary_wrong_sender_context_button_and_disabled(session):
    scope={"run_id":"run","buttons":{"approve":"APPROVE","deny":"DENY"}}
    row=prepare(session,execution_intent_id=intent_id(session, scope),scope=scope,expected_approver="owner",ttl_seconds=300,correlation_id="corr")
    request_approval(session,row.id,"wamid.request")
    assert handle_meta_control_event(session,event("interactive_canary_x_approve"),enabled=True)=="CANARY_NOT_HUMAN_AUTH"
    assert handle_meta_control_event(session,event("unknown"),enabled=True)=="UNKNOWN_BUTTON"
    assert handle_meta_control_event(session,event("approve",sender="other"),enabled=True)=="UNEXPECTED_APPROVER"
    assert handle_meta_control_event(session,event("approve",context="wrong"),enabled=True)=="UNRELATED_CONTEXT"
    assert handle_meta_control_event(session,event("approve"),enabled=False)=="GATE_DISABLED"

def test_expired_authorization_is_rejected(session):
    scope={"run_id":"run","buttons":{"approve":"APPROVE","deny":"DENY"}}
    now=datetime.now(UTC)
    row=prepare(session,execution_intent_id=intent_id(session, scope),scope=scope,expected_approver="owner",ttl_seconds=1,correlation_id="corr",now=now-timedelta(seconds=3))
    request_approval(session,row.id,"wamid.request")
    assert handle_meta_control_event(session,event("approve"),enabled=True,now=now)=="EXPIRED_OR_INVALID_STATE"
