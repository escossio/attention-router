from __future__ import annotations

from datetime import datetime, timezone
from inspect import signature
from pathlib import Path

import pytest
from sqlalchemy import select

from attention_router.adapters.channels import WhatsAppWebChannelBoundary
from attention_router.application.platform.authority import create_capability_grant
from attention_router.application.platform.context import build_context_snapshot
from attention_router.application.platform.devices import (
    announce_capabilities,
    bind_device,
    device_capability_authorized,
    register_device,
)
from attention_router.application.platform.entities import create_relationship, create_resource, set_entity_state
from attention_router.application.platform.events import create_canonical_event, normalize_inbound_event
from attention_router.application.platform.execution import execute_capability
from attention_router.application.platform.registry import (
    bind_provider,
    capability_and_version,
    ensure_default_tenant,
    matrix_status,
    register_provider_instance,
    resolve_capability_request,
    sync_capability_definitions,
    sync_platform_registry,
)
from attention_router.application.decision_pipeline import _recent_agent_turns
from attention_router.application.memory import ArchivedMessageInput, archive_message
from attention_router.application.repetition import check_text_repetition
from attention_router.application.services import create_interaction
from attention_router.core.capabilities import (
    CapabilityRequest,
    CapabilityResolutionStatus,
)
from attention_router.core.context import ContextCoreBuilder
from attention_router.core.devices import (
    DeviceCapabilityAnnouncement,
    DevicePlatform,
    DeviceRegistrationRequest,
    DeviceRole,
)
from attention_router.core.entities import EntityReference, ResourceSpec
from attention_router.core.events import (
    EventEnvelope,
    EventOrigin,
    OperatorAuthority,
    OwnerCommandUnauthorized,
)
from attention_router.core.providers import ProviderResult, ProviderRuntimeRegistry
from attention_router.core.tenancy import DEFAULT_TENANT_ID, TenantScopeError
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    ActorBindingRow,
    CapabilityGrantRow,
    InteractionRow,
    MemoryActorRow,
    MemoryClaimRow,
    OutboxMessageRow,
    ProviderDefinitionRow,
    TenantRow,
    TimelineEventRow,
)
from attention_router.provisioning.manifests import CapabilityManifest


TENANT_B = "00000000-0000-4000-8000-000000000002"


def _tenant(session, tenant_id=TENANT_B):
    row = TenantRow(
        id=tenant_id,
        slug=f"tenant_{tenant_id[-1]}",
        name=f"Tenant {tenant_id[-1]}",
        status="ACTIVE",
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row


def _binding(session, *, tenant_id, row_id, actor_key, external_id):
    row = ActorBindingRow(
        id=row_id,
        tenant_id=tenant_id,
        source="wwebjs",
        external_actor_id=external_id,
        actor_key=actor_key,
        actor_category="test",
        binding_metadata={},
        is_active=True,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row


def _coffee_manifest(version: int, state: str) -> CapabilityManifest:
    return CapabilityManifest.model_validate({
        "schema_version": "1",
        "capabilities": [{
            "canonical_name": "coffee.make",
            "version": version,
            "domain": "lab",
            "description": "Test-only generic capability.",
            "operation_type": "ACTION",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "required_permissions": ["coffee.make"],
            "required_provider_interface": "CoffeeMachineProvider",
            "sensitivity": "NORMAL",
            "side_effect": True,
            "default_approval_policy": "REQUIRES_APPROVAL",
            "availability_state": state,
            "metadata": {"fixture": True},
        }],
    })


class FakeCoffeeMachineProvider:
    interface_name = "CoffeeMachineProvider"

    def health(self) -> str:
        return "HEALTHY"

    def execute(self, capability, parameters):
        assert capability == "coffee.make"
        return ProviderResult(True, {"fixture": "completed"}, "FAKE_COFFEE_COMPLETED")


def test_manifest_sync_and_future_capabilities_fail_closed(session):
    result = sync_platform_registry(session)
    assert result["capabilities"]["created"] >= 20
    assert ensure_default_tenant(session).id == DEFAULT_TENANT_ID
    status = {item["capability"]: item for item in matrix_status(session)}
    assert status["conversation.reply"]["state"] == "OPERATIONAL"
    for name in ("location.current", "payment.verify", "artifact.temporary_access"):
        assert status[name]["state"] == "PROVIDER_MISSING"
        resolution = resolve_capability_request(
            session,
            CapabilityRequest(capability=name),
            tenant_id=DEFAULT_TENANT_ID,
            grantee_type="ACTOR",
            grantee_id="actor",
            policy_allows=True,
        )
        assert resolution.status == CapabilityResolutionStatus.KNOWN_BUT_UNAVAILABLE
        assert resolution.reason_code == "CAPABILITY_UNAVAILABLE"


def test_unknown_capability_and_provider_missing_are_deterministic(session):
    sync_platform_registry(session)
    unknown = resolve_capability_request(
        session,
        CapabilityRequest(capability="future.unknown"),
        tenant_id=DEFAULT_TENANT_ID,
        grantee_type="ACTOR",
        grantee_id="actor",
        policy_allows=True,
    )
    assert unknown.status == CapabilityResolutionStatus.UNKNOWN
    assert unknown.reason_code == "UNKNOWN_CAPABILITY"


def test_coffee_extensibility_uses_only_manifest_and_generic_provider(session):
    sync_platform_registry(session)
    sync_capability_definitions(session, _coffee_manifest(1, "PROVIDER_MISSING"))
    missing = resolve_capability_request(
        session,
        CapabilityRequest(capability="coffee.make", user_requested=True, confidence="high"),
        tenant_id=DEFAULT_TENANT_ID,
        grantee_type="ACTOR",
        grantee_id="coffee_tester",
        policy_allows=True,
    )
    assert missing.status == CapabilityResolutionStatus.KNOWN_BUT_UNAVAILABLE

    session.add(ProviderDefinitionRow(
        id=new_id(),
        canonical_name="fake_coffee_machine",
        interface_name="CoffeeMachineProvider",
        description="Test-only provider.",
        contract_version=1,
        created_at=now_utc(),
    ))
    session.flush()
    sync_capability_definitions(session, _coffee_manifest(2, "SANDBOX_PROVED"))
    provider = register_provider_instance(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        provider_definition_name="fake_coffee_machine",
        canonical_name="fake_coffee_candidate",
    )
    bind_provider(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        capability_name="coffee.make",
        provider_instance_id=provider.id,
    )
    provider.health = "DEGRADED"
    unavailable = resolve_capability_request(
        session,
        CapabilityRequest(capability="coffee.make"),
        tenant_id=DEFAULT_TENANT_ID,
        grantee_type="ACTOR",
        grantee_id="coffee_tester",
        policy_allows=True,
    )
    assert unavailable.status == CapabilityResolutionStatus.KNOWN_BUT_UNAVAILABLE
    provider.health = "HEALTHY"
    create_capability_grant(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        grantor_type="OPERATOR",
        grantor_id="candidate",
        grantee_type="ACTOR",
        grantee_id="coffee_tester",
        capability_name="coffee.make",
        provenance="test",
    )
    runtime = ProviderRuntimeRegistry()
    runtime.register(provider.id, FakeCoffeeMachineProvider())
    request = CapabilityRequest(capability="coffee.make", user_requested=True, confidence="high")
    held = execute_capability(
        session,
        request,
        tenant_id=DEFAULT_TENANT_ID,
        grantee_type="ACTOR",
        grantee_id="coffee_tester",
        policy_allows=True,
        runtime_registry=runtime,
    )
    assert held.status == "REQUIRES_APPROVAL"
    executed = execute_capability(
        session,
        request,
        tenant_id=DEFAULT_TENANT_ID,
        grantee_type="ACTOR",
        grantee_id="coffee_tester",
        policy_allows=True,
        runtime_registry=runtime,
        approval_granted=True,
    )
    assert executed.status == "EXECUTED"
    assert executed.reason_code == "FAKE_COFFEE_COMPLETED"
    core_paths = list(Path("attention_router/core").glob("*.py"))
    core_paths += list(Path("attention_router/application/platform").glob("*.py"))
    assert all("coffee.make" not in path.read_text(encoding="utf-8") for path in core_paths)


def test_tenant_isolation_for_actor_relationship_provider_and_grant(session):
    sync_platform_registry(session)
    _tenant(session)
    actor_a = _binding(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        row_id="actor-a-binding",
        actor_key="same-key",
        external_id="same@lid",
    )
    _binding(
        session,
        tenant_id=TENANT_B,
        row_id="actor-b-binding",
        actor_key="same-key",
        external_id="same@lid",
    )
    resource_b = create_resource(
        session,
        TENANT_B,
        ResourceSpec(resource_type="GENERIC", canonical_name="tenant-b-resource"),
    )
    with pytest.raises(TenantScopeError):
        create_relationship(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            source=EntityReference(entity_type="ACTOR", entity_id=actor_a.actor_key),
            target=EntityReference(entity_type="RESOURCE", entity_id=resource_b.id),
            relationship_type="owns",
        )

    provider_definition = session.scalar(
        select(ProviderDefinitionRow).where(ProviderDefinitionRow.canonical_name == "location")
    )
    provider_b = register_provider_instance(
        session,
        tenant_id=TENANT_B,
        provider_definition_name=provider_definition.canonical_name,
        canonical_name="tenant-b-location",
    )
    with pytest.raises(TenantScopeError):
        bind_provider(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            capability_name="location.current",
            provider_instance_id=provider_b.id,
        )

    definition_a, _ = capability_and_version(session, DEFAULT_TENANT_ID, "conversation.reply")
    session.add(CapabilityGrantRow(
        id=new_id(),
        tenant_id=TENANT_B,
        grantor_type="OWNER",
        grantor_id="owner-b",
        grantee_type="ACTOR",
        grantee_id="same-key",
        capability_id=definition_a.id,
        scope={},
        status="ACTIVE",
        valid_from=now_utc(),
        constraints_json={},
        provenance="test",
        created_at=now_utc(),
    ))
    session.flush()
    resolution = resolve_capability_request(
        session,
        CapabilityRequest(capability="conversation.reply"),
        tenant_id=DEFAULT_TENANT_ID,
        grantee_type="ACTOR",
        grantee_id="same-key",
        policy_allows=True,
    )
    # conversation.reply now has an explicit provider contract; without the
    # tenant's provider binding it is known but unavailable.
    assert resolution.status == CapabilityResolutionStatus.KNOWN_BUT_UNAVAILABLE


def test_context_never_reads_another_tenant_memory_or_timeline(session):
    sync_platform_registry(session)
    _tenant(session)
    actor_b = MemoryActorRow(
        id="memory-b",
        tenant_id=TENANT_B,
        actor_key="shared-actor",
        metadata_json={},
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(actor_b)
    session.flush()
    session.add(MemoryClaimRow(
        id="claim-b",
        subject_actor_id=actor_b.id,
        predicate="private.tenant_b",
        object_type="TEXT",
        object_text="not-visible",
        context={},
        confidence=1.0,
        sensitivity_class="NORMAL",
        source_quality="AUTHORITATIVE",
        status="ACTIVE",
        staleness_class="STABLE",
        first_observed_at=now_utc(),
        last_observed_at=now_utc(),
        created_at=now_utc(),
        updated_at=now_utc(),
    ))
    event = create_canonical_event(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        requested_origin=EventOrigin.SYSTEM_EVENT,
        event_type="context.test",
        payload_type="REFERENCE",
        correlation_id=new_id(),
        actor_id="shared-actor",
    )
    session.add(TimelineEventRow(
        id=new_id(),
        tenant_id=TENANT_B,
        actor_id="shared-actor",
        event_type="PRIVATE_B_EVENT",
        event_ref={},
        occurred_at=now_utc(),
        visibility="PRIVATE",
        provenance="test",
        metadata_json={},
    ))
    session.flush()
    snapshot = build_context_snapshot(session, event)
    assert snapshot.persistent_memory == []
    assert all(item["event_type"] != "PRIVATE_B_EVENT" for item in snapshot.relevant_historical_context)


def test_tenant_scopes_message_identity_recent_turns_and_repetition(session):
    _tenant(session)
    stamp = now_utc()
    common = {
        "source": "wwebjs",
        "source_account": "default",
        "thread_key": "shared-thread",
        "thread_type": "DIRECT",
        "source_message_id": "shared-message-id",
        "sender_key": "shared-actor",
        "sender_display_name": "Shared actor",
        "sent_at": stamp,
        "text": "tenant-scoped archive",
    }
    message_a, created_a = archive_message(
        session, ArchivedMessageInput(**common, tenant_id=DEFAULT_TENANT_ID)
    )
    message_b, created_b = archive_message(
        session, ArchivedMessageInput(**common, tenant_id=TENANT_B)
    )
    assert created_a and created_b
    assert message_a.id != message_b.id

    interaction_b = create_interaction(
        session,
        "message",
        "shared-contact",
        "Tenant B contact",
        "test",
        None,
        "tenant-b-private-turn",
        tenant_id=TENANT_B,
    )
    previous_b = session.get(InteractionRow, interaction_b["id"])
    session.add(
        OutboxMessageRow(
            id=new_id(),
            interaction_id=previous_b.id,
            action_type="agent_execution_text",
            destination="local_transport",
            payload={"text": "Sou a Andy, assistente virtual."},
            status="DONE",
            created_at=stamp,
            available_at=stamp,
            completed_at=stamp,
            attempt_count=1,
            idempotency_key=f"tenant-b:{new_id()}",
        )
    )
    current_a_data = create_interaction(
        session,
        "message",
        "shared-contact",
        "Tenant A contact",
        "test",
        None,
        "qual é o seu nome?",
        tenant_id=DEFAULT_TENANT_ID,
    )
    current_a = session.get(InteractionRow, current_a_data["id"])
    session.flush()

    recent = _recent_agent_turns(
        session, DEFAULT_TENANT_ID, "shared-contact", current_a.id
    )
    assert all(turn["content"] != "tenant-b-private-turn" for turn in recent)
    assert check_text_repetition(session, current_a).suppress is False


def test_owner_command_requires_authenticated_authority_and_origins_share_event_core(session):
    sync_platform_registry(session)
    with pytest.raises(OwnerCommandUnauthorized):
        create_canonical_event(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            requested_origin=EventOrigin.OWNER_COMMAND,
            event_type="command.text",
            payload_type="REFERENCE",
            payload_ref={"input_fixture": "[Andy] estou dormindo"},
            correlation_id=new_id(),
        )
    external = create_canonical_event(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        requested_origin=EventOrigin.EXTERNAL_INBOUND,
        event_type="message",
        payload_type="REFERENCE",
        payload_ref={"input_fixture": "[Andy] estou dormindo"},
        correlation_id=new_id(),
    )
    assert external.origin == EventOrigin.EXTERNAL_INBOUND.value
    authority = OperatorAuthority(
        operator_actor_id="owner",
        tenant_id=DEFAULT_TENANT_ID,
        authenticated=True,
        roles=["OWNER"],
    )
    owner = create_canonical_event(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        requested_origin=EventOrigin.OWNER_COMMAND,
        event_type="command.text",
        payload_type="REFERENCE",
        correlation_id=new_id(),
        operator_authority=authority,
    )
    assert owner.origin == EventOrigin.OWNER_COMMAND.value
    presence = resolve_capability_request(
        session,
        CapabilityRequest(capability="presence.set", user_requested=True),
        tenant_id=DEFAULT_TENANT_ID,
        grantee_type="ACTOR",
        grantee_id="owner",
        policy_allows=True,
    )
    assert presence.status == CapabilityResolutionStatus.KNOWN_BUT_UNAVAILABLE
    for origin in (
        EventOrigin.SYSTEM_EVENT,
        EventOrigin.PROVIDER_EVENT,
        EventOrigin.DEVICE_EVENT,
        EventOrigin.SCHEDULED_EVENT,
        EventOrigin.INTERNAL_EVENT,
    ):
        row = create_canonical_event(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            requested_origin=origin,
            event_type="contract.test",
            payload_type="REFERENCE",
            correlation_id=new_id(),
        )
        assert row.origin == origin.value


def test_context_core_composes_injected_sources_without_lab_session_dependency():
    class Source:
        def __init__(self, name):
            self.name = name

        def retrieve(self, tenant_id, actor_id, limit):
            assert tenant_id == DEFAULT_TENANT_ID
            assert actor_id == "actor"
            assert limit == 3
            return [{"source": self.name}]

    event = EventEnvelope(
        origin=EventOrigin.SYSTEM_EVENT,
        event_type="context.contract",
        actor_id="actor",
        payload_type="REFERENCE",
        occurred_at=now_utc(),
        received_at=now_utc(),
        correlation_id=new_id(),
    )
    sources = {name: Source(name) for name in (
        "recent", "historical", "memory", "state", "facts",
        "capabilities", "decisions", "authority",
    )}
    snapshot = ContextCoreBuilder(**sources).build(event, limit=3)
    assert snapshot.recent_conversation == [{"source": "recent"}]
    assert snapshot.relevant_historical_context == [{"source": "historical"}]
    assert snapshot.persistent_memory == [{"source": "memory"}]
    assert snapshot.current_operational_state == [{"source": "state"}]
    assert snapshot.relevant_facts == [{"source": "facts"}]
    assert snapshot.available_capabilities == [{"source": "capabilities"}]
    assert snapshot.relevant_decisions == [{"source": "decisions"}]
    assert snapshot.effective_authority == [{"source": "authority"}]
    assert "lab" not in signature(ContextCoreBuilder).parameters


def test_inbound_event_bridge_is_idempotent_and_does_not_copy_raw_payload(session):
    from attention_router.infrastructure.models import InboundEventRow

    event = InboundEventRow(
        id="legacy-inbound",
        tenant_id=DEFAULT_TENANT_ID,
        source="wwebjs",
        external_event_id="legacy-external",
        event_type="message",
        payload={"channel": "whatsapp", "content": "raw fixture"},
        payload_hash="hash",
        received_at=now_utc(),
        status="PROCESSED",
        correlation_id=new_id(),
    )
    session.add(event)
    session.flush()
    first = normalize_inbound_event(session, event, actor_id="actor")
    second = normalize_inbound_event(session, event, actor_id="actor")
    assert first.id == second.id
    assert first.payload_ref == {"inbound_event_id": event.id}
    assert "raw fixture" not in str(first.payload_ref)


def test_device_availability_is_not_authorization(session):
    sync_platform_registry(session)
    actor = _binding(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        row_id="device-actor-binding",
        actor_key="device-owner",
        external_id="device-owner@lid",
    )
    registration = register_device(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        request=DeviceRegistrationRequest(
            canonical_name="android-test",
            platform=DevicePlatform.ANDROID,
            roles=[DeviceRole.CLIENT, DeviceRole.CAPABILITY_NODE],
            identity_type="TEST_ATTESTATION",
            identity_reference="opaque-test-reference",
        ),
    )
    bind_device(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        device_id=registration.device_id,
        actor_key=actor.actor_key,
    )
    announce_capabilities(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        device_id=registration.device_id,
        announcement=DeviceCapabilityAnnouncement(
            capabilities=["device.battery"],
            announced_at=datetime.now(timezone.utc),
        ),
    )
    assert not device_capability_authorized(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        device_id=registration.device_id,
        capability_name="device.battery",
    )
    create_capability_grant(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        grantor_type="OWNER",
        grantor_id="owner",
        grantee_type="DEVICE",
        grantee_id=registration.device_id,
        capability_name="device.battery",
        provenance="test",
    )
    assert device_capability_authorized(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        device_id=registration.device_id,
        capability_name="device.battery",
    )


def test_state_is_versioned_and_channel_core_uses_binding_not_jid(session):
    actor = _binding(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        row_id="state-actor-binding",
        actor_key="state-owner",
        external_id="state-owner@lid",
    )
    subject = EntityReference(entity_type="ACTOR", entity_id=actor.actor_key)
    first = set_entity_state(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        subject=subject,
        namespace="presence",
        key="mode",
        value={"value": "available"},
        source="test",
        expected_version=0,
    )
    assert first.version == 1
    second = set_entity_state(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        subject=subject,
        namespace="presence",
        key="mode",
        value={"value": "busy"},
        source="test",
        expected_version=1,
    )
    assert second.version == 2
    with pytest.raises(ValueError, match="STATE_VERSION_CONFLICT"):
        set_entity_state(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            subject=subject,
            namespace="presence",
            key="mode",
            value={"value": "stale"},
            source="test",
            expected_version=1,
        )
    target = WhatsAppWebChannelBoundary.target(actor.id)
    assert target.binding_id == actor.id
    assert "lid" not in target.binding_id
