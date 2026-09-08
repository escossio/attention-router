"""TRANSPORT_HOST-HA16: frozen production authority through Meta human control only."""
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from attention_router.adapters.meta_whatsapp import MetaWhatsAppInboundAdapter
from attention_router.infrastructure.models import (
    AgentExecutionIntentRow,
    BoundedRunAuthorizationRow,
    EffectBudgetRow,
    ExecutionLeaseRow,
    ExecutionIntentRow,
    HumanExecutionAuthorizationRow,
    OutboxMessageRow,
    ScenarioRunRow,
)
from attention_router.platform.human_approval_integration import (
    build_production_human_approval_request,
    mark_production_human_approval_requested,
    prepare_production_human_approval,
)
from attention_router.platform.human_authorization_control_gate import handle_meta_control_event
from attention_router.platform.production_bridge import validate_frozen_execution_intent_authority
from tests.integration.test_postgres_10_5c_6i1a_r2_bridge_adversarial import _parent
from tests.meta_fixtures import meta_text_message


pytestmark = pytest.mark.postgres
APPROVER = "15550000001"


def _event(*, wamid: str, sender: str, context: str, button: str):
    payload = meta_text_message(wamid, sender, "ignored")
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message.pop("text")
    message.update({
        "type": "interactive",
        "context": {"id": context},
        "interactive": {"type": "button_reply", "button_reply": {
            "id": button, "title": "Aprovar",
        }},
    })
    return MetaWhatsAppInboundAdapter().normalize(payload)


def _prepare_pending(session, parent=None, *, approver=APPROVER, now=None):
    parent = parent or _parent(session)
    auth = prepare_production_human_approval(
        session,
        execution_intent_id=parent.id,
        expected_approver=approver,
        ttl_seconds=120,
        correlation_id=uuid4().hex,
        now=now,
    )
    request = build_production_human_approval_request(session, authorization_id=auth.id, now=now)
    mark_production_human_approval_requested(
        session, authorization_id=auth.id,
        request_wamid=f"wamid.request.{uuid4().hex}", now=now,
    )
    session.flush()
    return parent, auth, request


def _button(auth, action):
    return next(button for button, value in auth.scope["buttons"].items() if value == action)


def _counts(session, parent):
    return {
        "agent": session.scalar(select(func.count()).select_from(AgentExecutionIntentRow).where(
            AgentExecutionIntentRow.execution_intent_id == parent.id)),
        "run": session.scalar(select(func.count()).select_from(ScenarioRunRow)),
        "outbox": session.scalar(select(func.count()).select_from(OutboxMessageRow)),
        "budget": session.scalar(select(func.count()).select_from(EffectBudgetRow)),
        "lease": session.scalar(select(func.count()).select_from(ExecutionLeaseRow)),
        "bra": session.scalar(select(func.count()).select_from(BoundedRunAuthorizationRow)),
    }


def test_ha01_prepare_hea_from_exact_frozen_execution_intent(Session):
    with Session() as session:
        parent = _parent(session)
        auth = prepare_production_human_approval(
            session, execution_intent_id=parent.id, expected_approver=APPROVER,
            ttl_seconds=120, correlation_id=uuid4().hex,
        )
        assert auth.execution_intent_id == parent.id
        assert auth.execution_intent_fingerprint == parent.scope_fingerprint
        assert auth.expected_approver == APPROVER
        assert auth.issued_at < auth.expires_at <= parent.expires_at


def test_ha02_pending_request_remains_exactly_bound(Session):
    with Session() as session:
        parent, auth, request = _prepare_pending(session)
        auth_id, parent_id, fingerprint = auth.id, parent.id, parent.scope_fingerprint
        session.commit()
    with Session() as observer:
        stored = observer.get(HumanExecutionAuthorizationRow, auth_id)
        assert stored.state == "PENDING_HUMAN_APPROVAL"
        assert (stored.execution_intent_id, stored.execution_intent_fingerprint) == (parent_id, fingerprint)
        assert stored.correlation_id == request["correlation_id"]
        assert stored.request_wamid


def test_ha03_approve_valid_normalized_meta_request(Session):
    with Session() as session:
        parent, auth, _ = _prepare_pending(session)
        event = _event(wamid="wamid.ha03", sender=APPROVER, context=auth.request_wamid,
                       button=_button(auth, "APPROVE"))
        assert handle_meta_control_event(session, event, enabled=True) == "APPROVE"
        assert session.get(HumanExecutionAuthorizationRow, auth.id).state == "APPROVED"
        assert session.get(HumanExecutionAuthorizationRow, auth.id).decision_inbound_wamid == "wamid.ha03"
        assert session.get(ExecutionIntentRow, parent.id).state == "FROZEN"


def test_ha04_deny_valid_normalized_meta_request(Session):
    with Session() as session:
        _, auth, _ = _prepare_pending(session)
        event = _event(wamid="wamid.ha04", sender=APPROVER, context=auth.request_wamid,
                       button=_button(auth, "DENY"))
        assert handle_meta_control_event(session, event, enabled=True) == "DENY"
        assert session.get(HumanExecutionAuthorizationRow, auth.id).state == "DENIED"


def test_ha05_wrong_approver_rejected(Session):
    with Session() as session:
        _, auth, _ = _prepare_pending(session)
        event = _event(wamid="wamid.ha05", sender="15550000999", context=auth.request_wamid,
                       button=_button(auth, "APPROVE"))
        assert handle_meta_control_event(session, event, enabled=True) == "UNEXPECTED_APPROVER"
        assert session.get(HumanExecutionAuthorizationRow, auth.id).state == "PENDING_HUMAN_APPROVAL"


def test_ha06_wrong_context_rejected(Session):
    with Session() as session:
        _, auth, _ = _prepare_pending(session)
        event = _event(wamid="wamid.ha06", sender=APPROVER, context="wamid.other",
                       button=_button(auth, "APPROVE"))
        assert handle_meta_control_event(session, event, enabled=True) == "UNRELATED_CONTEXT"
        assert session.get(HumanExecutionAuthorizationRow, auth.id).state == "PENDING_HUMAN_APPROVAL"


def test_ha07_wrong_opaque_button_rejected(Session):
    with Session() as session:
        _, auth, _ = _prepare_pending(session)
        event = _event(wamid="wamid.ha07", sender=APPROVER, context=auth.request_wamid,
                       button=uuid4().hex)
        assert handle_meta_control_event(session, event, enabled=True) == "UNKNOWN_BUTTON"
        assert session.get(HumanExecutionAuthorizationRow, auth.id).state == "PENDING_HUMAN_APPROVAL"


def test_ha08_authority_drift_at_decision_is_rejected(Session):
    with Session() as session:
        parent, auth, _ = _prepare_pending(session)
        policy_id = parent.scope["policies"][0]["id"]
        session.execute(
            __import__("sqlalchemy").text("UPDATE policy_versions SET status='RETIRED' WHERE id=:id"),
            {"id": policy_id},
        )
        event = _event(wamid="wamid.ha08", sender=APPROVER, context=auth.request_wamid,
                       button=_button(auth, "APPROVE"))
        assert handle_meta_control_event(session, event, enabled=True) == "PRODUCTION_DEPENDENCY_INACTIVE"
        assert session.get(HumanExecutionAuthorizationRow, auth.id).state == "PENDING_HUMAN_APPROVAL"


def test_ha09_execution_intent_expiry_is_rejected(Session):
    with Session() as session:
        parent, auth, _ = _prepare_pending(session)
        event = _event(wamid="wamid.ha09", sender=APPROVER, context=auth.request_wamid,
                       button=_button(auth, "APPROVE"))
        assert handle_meta_control_event(session, event, enabled=True, now=parent.expires_at) == "EXECUTION_INTENT_EXPIRED"
        assert session.get(HumanExecutionAuthorizationRow, auth.id).state == "PENDING_HUMAN_APPROVAL"


def test_ha10_hea_expiry_is_rejected(Session):
    with Session() as session:
        _, auth, _ = _prepare_pending(session)
        event = _event(wamid="wamid.ha10", sender=APPROVER, context=auth.request_wamid,
                       button=_button(auth, "APPROVE"))
        assert handle_meta_control_event(session, event, enabled=True, now=auth.expires_at) == "EXPIRED_OR_INVALID_STATE"
        assert session.get(HumanExecutionAuthorizationRow, auth.id).state == "PENDING_HUMAN_APPROVAL"


def test_ha11_decision_replay_is_idempotent(Session):
    with Session() as session:
        _, auth, _ = _prepare_pending(session)
        event = _event(wamid="wamid.ha11", sender=APPROVER, context=auth.request_wamid,
                       button=_button(auth, "APPROVE"))
        assert handle_meta_control_event(session, event, enabled=True) == "APPROVE"
        assert handle_meta_control_event(session, event, enabled=True) == "DUPLICATE"
        decided = session.scalar(select(func.count()).select_from(HumanExecutionAuthorizationRow).where(
            HumanExecutionAuthorizationRow.id == auth.id,
            HumanExecutionAuthorizationRow.state == "APPROVED",
        ))
        assert decided == 1


def test_ha12_cross_intent_reuse_is_rejected(Session):
    with Session() as session:
        _, auth_a, _ = _prepare_pending(session)
        _, auth_b, _ = _prepare_pending(session)
        event = _event(wamid="wamid.ha12", sender=APPROVER, context=auth_b.request_wamid,
                       button=_button(auth_a, "APPROVE"))
        assert handle_meta_control_event(session, event, enabled=True) == "UNKNOWN_BUTTON"
        assert session.get(HumanExecutionAuthorizationRow, auth_b.id).state == "PENDING_HUMAN_APPROVAL"


def test_ha13_hea_metadata_cannot_widen_authority(Session):
    with Session() as session:
        _, auth, _ = _prepare_pending(session)
        auth.scope = {**auth.scope, "target": "widened", "transport": "email",
                      "limits": {"max_effects": 999}, "policy": "widened"}
        event = _event(wamid="wamid.ha13", sender=APPROVER, context=auth.request_wamid,
                       button=_button(auth, "APPROVE"))
        assert handle_meta_control_event(session, event, enabled=True) == "APPROVE"
        assert session.get(HumanExecutionAuthorizationRow, auth.id).state == "APPROVED"


def test_ha14_approval_remains_stop(Session):
    with Session() as session:
        parent, auth, _ = _prepare_pending(session)
        before = _counts(session, parent)
        event = _event(wamid="wamid.ha14", sender=APPROVER, context=auth.request_wamid,
                       button=_button(auth, "APPROVE"))
        assert handle_meta_control_event(session, event, enabled=True) == "APPROVE"
        session.commit()
    with Session() as observer:
        assert observer.get(HumanExecutionAuthorizationRow, auth.id).state == "APPROVED"
        assert observer.get(ExecutionIntentRow, parent.id).state == "FROZEN"
        assert _counts(observer, observer.get(ExecutionIntentRow, parent.id)) == before


def test_ha15_denial_remains_stop(Session):
    with Session() as session:
        parent, auth, _ = _prepare_pending(session)
        before = _counts(session, parent)
        event = _event(wamid="wamid.ha15", sender=APPROVER, context=auth.request_wamid,
                       button=_button(auth, "DENY"))
        assert handle_meta_control_event(session, event, enabled=True) == "DENY"
        assert _counts(session, parent) == before


def test_ha16_approved_hea_remains_exactly_linked_and_request_is_buildable(Session):
    with Session() as session:
        parent, auth, request = _prepare_pending(session)
        button_ids = [item["reply"]["id"] for item in request["payload"]["interactive"]["action"]["buttons"]]
        assert {item["reply"]["title"] for item in request["payload"]["interactive"]["action"]["buttons"]} == {"Aprovar", "Negar"}
        assert len(button_ids) == 2 and all(parent.id not in value and parent.scope_fingerprint not in value for value in button_ids)
        assert request["correlation_id"] == auth.correlation_id
        event = _event(wamid="wamid.ha16", sender=APPROVER, context=auth.request_wamid,
                       button=_button(auth, "APPROVE"))
        assert handle_meta_control_event(session, event, enabled=True) == "APPROVE"
        authority, _ = validate_frozen_execution_intent_authority(
            session, parent=parent, expected_fingerprint=auth.execution_intent_fingerprint,
            effective_response_snapshot="ok",
        )
        assert authority.capability == "conversation.reply"
        assert auth.execution_intent_fingerprint == parent.scope_fingerprint
