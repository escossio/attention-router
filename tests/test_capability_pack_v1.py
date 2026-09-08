from datetime import timedelta

from sqlalchemy import select

from attention_router.application.platform.capability_pack import (
    CAPABILITY_PROVIDER,
    execute_owner_capability,
    process_due_scheduled_events,
    provision_internal_providers,
    internal_runtime_registry,
)
from attention_router.application.platform.registry import matrix_status, resolve_capability_request
from attention_router.core.capabilities import CapabilityRequest, CapabilityResolutionStatus
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    ActorBindingRow,
    CapabilityGrantRow,
    ReminderRow,
    TimelineEventRow,
)


OWNER = "owner"
TENANT_B = "00000000-0000-4000-8000-000000000099"


def _owner(session, tenant_id=DEFAULT_TENANT_ID, actor=OWNER):
    row = ActorBindingRow(
        id=new_id(),
        tenant_id=tenant_id,
        source="test",
        external_actor_id=f"{actor}@test",
        actor_key=actor,
        actor_category="owner",
        binding_metadata={"owner": True},
        is_active=True,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row


def _run(session, capability, parameters, actor=OWNER, correlation_id="pack-test"):
    return execute_owner_capability(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=actor,
        request=CapabilityRequest(
            capability=capability, parameters=parameters, user_requested=True, confidence="high"
        ),
        correlation_id=correlation_id,
    )


def test_pack_materializes_all_internal_bindings_and_discovery(session):
    _owner(session)
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    status = {item["capability"]: item for item in matrix_status(session)}
    assert set(CAPABILITY_PROVIDER) <= set(status)
    for name in CAPABILITY_PROVIDER:
        assert status[name]["state"] == "OPERATIONAL"
        assert status[name]["bound_provider"] == CAPABILITY_PROVIDER[name]
    result = _run(session, "capability.inspect", {"capability_name": "location.current"})
    assert result.status == "EXECUTED"
    assert result.result["availability_state"] == "PROVIDER_MISSING"
    assert result.result["bound_provider_status"] == "PROVIDER_MISSING"
    listed = _run(session, "capability.list", {})
    assert any(item["capability_name"] == "presence.set" for item in listed.result["capabilities"])


def test_conversation_reply_binding_is_controlled_and_non_direct_send(session):
    _owner(session)
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    resolution = resolve_capability_request(
        session,
        CapabilityRequest(capability="conversation.reply"),
        tenant_id=DEFAULT_TENANT_ID,
        grantee_type="ACTOR",
        grantee_id=OWNER,
        policy_allows=True,
        owner_authorized=True,
    )
    assert resolution.provider_instance_id is not None
    runtime = internal_runtime_registry(session, DEFAULT_TENANT_ID)
    provider = runtime.resolve(resolution.provider_instance_id, "InternalReplyProvider")
    result = provider.execute("conversation.reply", {"response_text": "dry-run"})
    assert result.reason_code == "EXECUTION_INTENT_REQUIRED"


def test_presence_commitment_and_timeline_are_tenant_scoped(session):
    _owner(session)
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    presence = _run(session, "presence.set", {"state": "sleeping"})
    assert presence.status == "EXECUTED"
    created = _run(
        session,
        "commitment.create",
        {"summary": "Enviar relatório", "beneficiary_actor_id": "regiane"},
    )
    commitment_id = created.result["commitment_id"]
    assert (
        _run(session, "commitment.complete", {"commitment_id": commitment_id}).result["status"]
        == "COMPLETED"
    )
    timeline = _run(session, "timeline.query", {"actor_id": OWNER})
    assert timeline.result["coverage"] == "recorded_timeline_only"
    assert {item["event_type"] for item in timeline.result["events"]} >= {
        "STATE_CHANGED",
        "COMMITMENT_COMPLETED",
    }


def test_reminder_is_idempotent_then_fires_once_as_scheduled_event(session):
    _owner(session)
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    trigger = (now_utc() - timedelta(minutes=1)).isoformat()
    first = _run(
        session,
        "reminder.create",
        {"summary": "Ligar para Regiane", "trigger_at": trigger},
        correlation_id="same-reminder",
    )
    second = _run(
        session,
        "reminder.create",
        {"summary": "Ligar para Regiane", "trigger_at": trigger},
        correlation_id="same-reminder",
    )
    assert first.result["reminder_id"] == second.result["reminder_id"]
    assert process_due_scheduled_events(session, now=now_utc()) == 1
    assert process_due_scheduled_events(session, now=now_utc()) == 0
    row = session.get(ReminderRow, first.result["reminder_id"])
    assert row.status == "FIRED"
    events = session.scalars(
        select(TimelineEventRow).where(TimelineEventRow.event_type == "REMINDER_FIRED")
    ).all()
    assert len(events) == 1


def test_grant_mutation_requires_authenticated_owner_and_blocks_self_escalation(session):
    _owner(session)
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    denied = resolve_capability_request(
        session,
        CapabilityRequest(capability="grant.create"),
        tenant_id=DEFAULT_TENANT_ID,
        grantee_type="ACTOR",
        grantee_id="ordinary",
        policy_allows=True,
    )
    assert denied.status == CapabilityResolutionStatus.AVAILABLE_NOT_AUTHORIZED
    self_grant = _run(
        session, "grant.create", {"grantee_id": OWNER, "capability_name": "presence.set"}
    )
    assert self_grant.status == "FAILED"
    assert self_grant.reason_code == "GRANTEE_SELF_ESCALATION_DENIED"
    created = _run(
        session,
        "grant.create",
        {
            "grantee_id": "regiane",
            "capability_name": "presence.set",
            "valid_until": (now_utc() + timedelta(days=1)).isoformat(),
        },
    )
    assert created.status == "EXECUTED"
    revoked = _run(session, "grant.revoke", {"grant_id": created.result["grant_id"]})
    assert revoked.result["status"] == "REVOKED"
    assert session.get(CapabilityGrantRow, created.result["grant_id"]).revoked_by == OWNER
