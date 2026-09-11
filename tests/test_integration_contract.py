import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from attention_router.application.platform.integrations import (
    artifact_contract_to_input,
    capability_contract_to_request,
    inbound_contract_to_envelope,
)
from attention_router.contracts.integration import (
    ArtifactReceiptContract,
    CapabilityInvocationContract,
    ChannelDeliveryContract,
    ExternalActorRef,
    ExternalThreadRef,
    InboundIntegrationEvent,
    IntegrationErrorClass,
    IntegrationKind,
    IntegrationResultContract,
    IntegrationResultStatus,
    IntegrationSource,
    IntegrationThreadKind,
    integration_contract_json_schema,
)
from attention_router.core.events import EventOrigin


NOW = datetime(2026, 9, 11, 17, 0, tzinfo=timezone.utc)
TENANT = "00000000-0000-4000-8000-000000000001"


def _source(kind: IntegrationKind, name: str) -> IntegrationSource:
    return IntegrationSource(
        kind=kind,
        name=name,
        instance_id="synthetic-instance",
        account_id="synthetic-account",
    )


def test_inbound_contract_requires_explicit_tenant_and_rejects_extra_fields():
    payload = {
        "source": _source(IntegrationKind.CHANNEL, "channel.email"),
        "external_event_id": "mail-1",
        "event_type": "message",
        "payload_type": "EMAIL_REFERENCE",
        "occurred_at": NOW,
        "received_at": NOW,
        "idempotency_key": "channel.email:mail-1",
        "correlation_id": "corr-1",
    }
    with pytest.raises(ValidationError):
        InboundIntegrationEvent.model_validate(payload)

    payload["tenant_id"] = TENANT
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        InboundIntegrationEvent.model_validate(payload)


def test_inbound_bridge_keeps_external_identity_out_of_canonical_actor():
    event = InboundIntegrationEvent(
        tenant_id=TENANT,
        source=_source(IntegrationKind.CHANNEL, "channel.email"),
        external_event_id="mail-2",
        event_type="message",
        actor=ExternalActorRef(
            external_actor_id="external-sender@example.invalid",
            display_name="Synthetic Sender",
        ),
        thread=ExternalThreadRef(
            external_thread_id="provider-thread-99",
            kind=IntegrationThreadKind.THREAD,
        ),
        payload_type="EMAIL_REFERENCE",
        payload_ref={"message_ref": "opaque-message-ref"},
        artifact_ids=["artifact-a"],
        occurred_at=NOW,
        received_at=NOW,
        idempotency_key="channel.email:mail-2",
        correlation_id="corr-2",
        metadata_sanitized={"subject_present": True},
    )

    envelope = inbound_contract_to_envelope(event, resolved_actor_id="actor-123")

    assert envelope.tenant_id == TENANT
    assert envelope.origin == EventOrigin.EXTERNAL_INBOUND
    assert envelope.actor_id == "actor-123"
    assert envelope.channel == "channel.email"
    assert envelope.metadata_sanitized["artifact_count"] == 1
    assert envelope.metadata_sanitized["thread_kind"] == "THREAD"
    serialized_metadata = json.dumps(envelope.metadata_sanitized)
    assert "external-sender@example.invalid" not in serialized_metadata
    assert "provider-thread-99" not in serialized_metadata


def test_artifact_contract_reuses_artifact_plane_receipt_input():
    contract = ArtifactReceiptContract(
        tenant_id=TENANT,
        source=_source(IntegrationKind.CHANNEL, "channel.email"),
        external_receipt_id="attachment-1",
        content_sha256="a" * 64,
        artifact_kind="spreadsheet",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=1234,
        storage_provider="s3",
        storage_reference="tenant/object/ref",
        received_at=NOW,
        sender=ExternalActorRef(external_actor_id="external-sender"),
        original_filename="synthetic-report.xlsx",
    )

    item = artifact_contract_to_input(contract, resolved_sender_actor_id="actor-123")

    assert item.tenant_id == TENANT
    assert item.source_channel == "channel.email"
    assert item.source_account == "synthetic-account"
    assert item.sender_actor_id == "actor-123"
    assert item.artifact_kind == "SPREADSHEET"
    assert item.storage_reference == "tenant/object/ref"


def test_capability_contract_is_provider_neutral_and_does_not_grant_authority():
    contract = CapabilityInvocationContract(
        tenant_id=TENANT,
        integration=_source(IntegrationKind.CAPABILITY, "provider.google_calendar"),
        capability="calendar.schedule",
        target={"actor_id": "actor-123"},
        parameters={"starts_at": "2026-09-12T14:00:00-03:00"},
        user_requested=True,
        confidence="high",
        idempotency_key="calendar.schedule:synthetic-1",
        correlation_id="corr-3",
    )

    request = capability_contract_to_request(contract)

    assert request.capability == "calendar.schedule"
    assert request.user_requested is True
    assert request.parameters["starts_at"].startswith("2026-09-12")
    assert not hasattr(request, "execution_allowed")

    with pytest.raises(ValidationError):
        CapabilityInvocationContract(
            **{
                **contract.model_dump(),
                "integration": _source(IntegrationKind.CHANNEL, "channel.email"),
            }
        )


def test_channel_delivery_requires_channel_integration():
    with pytest.raises(ValidationError):
        ChannelDeliveryContract(
            tenant_id=TENANT,
            integration=_source(IntegrationKind.CAPABILITY, "provider.calendar"),
            target_binding_id="binding-1",
            payload_type="TEXT_REFERENCE",
            idempotency_key="send-1",
            correlation_id="corr-4",
        )


def test_result_contract_standardizes_retry_and_permanent_failure():
    retry = IntegrationResultContract(
        tenant_id=TENANT,
        integration=_source(IntegrationKind.CHANNEL, "channel.email"),
        request_id="send-1",
        correlation_id="corr-4",
        status=IntegrationResultStatus.RETRYABLE_FAILURE,
        reason_code="PROVIDER_RATE_LIMIT",
        error_class=IntegrationErrorClass.RATE_LIMIT,
        retry_after_seconds=30,
    )
    assert retry.retry_after_seconds == 30

    with pytest.raises(ValidationError):
        IntegrationResultContract(
            tenant_id=TENANT,
            integration=_source(IntegrationKind.CHANNEL, "channel.email"),
            request_id="send-2",
            correlation_id="corr-5",
            status=IntegrationResultStatus.PERMANENT_FAILURE,
            reason_code="INVALID_TARGET",
        )

    with pytest.raises(ValidationError):
        IntegrationResultContract(
            tenant_id=TENANT,
            integration=_source(IntegrationKind.CHANNEL, "channel.email"),
            request_id="send-3",
            correlation_id="corr-6",
            status=IntegrationResultStatus.SUCCEEDED,
            reason_code="OK",
            error_class=IntegrationErrorClass.UNKNOWN,
        )


def test_published_language_neutral_schema_is_in_sync():
    root = Path(__file__).resolve().parents[1]
    published = json.loads(
        (root / "contracts/integration/v1/integration-contract.schema.json").read_text()
    )
    assert published == integration_contract_json_schema()
    assert published["$id"] == "urn:attention-router:integration-contract:v1"
    assert published["discriminator"]["propertyName"] == "contract_type"
