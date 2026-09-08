"""L0 proof for the real intent -> release -> provider -> outbox boundary."""

from attention_router.application import execution
from attention_router.application.platform.capability_pack import internal_runtime_registry
from attention_router.application.platform.execution import execute_capability
from attention_router.config import settings
from attention_router.core.capabilities import CapabilityRequest
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import OutboxMessageRow

from tests.test_execution import _intent


def test_l0_reply_uses_official_intent_release_and_real_outbox(session, monkeypatch):
    intent = _intent(session)
    monkeypatch.setattr(settings, "external_delivery_enabled", True)

    # This is the official release path; only the isolated test process has
    # external delivery enabled, and no transport worker exists in this test.
    execution.release_intent(session, intent.id, transport_ready=True)
    from attention_router.application.platform.registry import ensure_default_tenant
    ensure_default_tenant(session)
    runtime = internal_runtime_registry(session, DEFAULT_TENANT_ID)
    request = CapabilityRequest(
        capability="conversation.reply",
        parameters={
            "response_text": "L0 isolated response",
            "_execution": {"execution_intent_id": intent.id},
        },
    )
    result = execute_capability(
        session,
        request,
        tenant_id=DEFAULT_TENANT_ID,
        grantee_type="ACTOR",
        grantee_id="owner",
        policy_allows=True,
        runtime_registry=runtime,
        approval_granted=True,
        owner_authorized=True,
    )

    assert result.status == "EXECUTED", result.reason_code
    assert result.reason_code == "DELEGATED"
    outbox = session.query(OutboxMessageRow).filter_by(execution_intent_id=intent.id).one()
    assert outbox.status == "PENDING"
    assert outbox.destination == "local_transport"
