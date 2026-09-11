"""Bridges the neutral Integration Contract into existing Attention Router domains."""

from __future__ import annotations

from attention_router.application.platform.artifacts import ArtifactReceiptInput
from attention_router.contracts.integration import (
    ArtifactReceiptContract,
    CapabilityInvocationContract,
    InboundIntegrationEvent,
    IntegrationKind,
)
from attention_router.core.capabilities import CapabilityRequest
from attention_router.core.events import EventEnvelope, EventOrigin


def inbound_contract_to_envelope(
    event: InboundIntegrationEvent,
    *,
    resolved_actor_id: str | None = None,
    resource_id: str | None = None,
) -> EventEnvelope:
    """Translate an authenticated/resolved integration event into the canonical event."""
    origin = (
        EventOrigin.EXTERNAL_INBOUND
        if event.source.kind == IntegrationKind.CHANNEL
        else EventOrigin.PROVIDER_EVENT
    )
    metadata = {
        **event.metadata_sanitized,
        "integration_name": event.source.name,
        "integration_kind": event.source.kind.value,
        "integration_account_present": event.source.account_id is not None,
        "artifact_count": len(event.artifact_ids),
    }
    if event.thread is not None:
        metadata["thread_kind"] = event.thread.kind.value
        metadata["external_thread_present"] = True

    return EventEnvelope(
        tenant_id=event.tenant_id,
        origin=origin,
        event_type=event.event_type,
        actor_id=resolved_actor_id,
        resource_id=resource_id,
        channel=event.source.name[:80],
        payload_type=event.payload_type,
        payload_ref=dict(event.payload_ref),
        occurred_at=event.occurred_at,
        received_at=event.received_at,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        metadata_sanitized=metadata,
    )


def artifact_contract_to_input(
    contract: ArtifactReceiptContract,
    *,
    resolved_sender_actor_id: str | None = None,
) -> ArtifactReceiptInput:
    """Reuse Artifact Plane identity/provenance instead of introducing another file store."""
    return ArtifactReceiptInput(
        tenant_id=contract.tenant_id,
        content_sha256=contract.content_sha256,
        artifact_kind=contract.artifact_kind,
        mime_type=contract.mime_type,
        size_bytes=contract.size_bytes,
        storage_provider=contract.storage_provider,
        storage_reference=contract.storage_reference,
        source_channel=contract.source.name,
        external_receipt_id=contract.external_receipt_id,
        received_at=contract.received_at,
        source_account=contract.source.account_id or contract.source.instance_id,
        sender_actor_id=resolved_sender_actor_id,
        original_filename=contract.original_filename,
        artifact_metadata=dict(contract.artifact_metadata),
        receipt_metadata={
            **contract.receipt_metadata,
            "integration_instance_id": contract.source.instance_id,
            "integration_kind": contract.source.kind.value,
        },
    )


def capability_contract_to_request(
    contract: CapabilityInvocationContract,
) -> CapabilityRequest:
    """Reuse semantic capability requests. Conversion never grants execution authority."""
    return CapabilityRequest(
        capability=contract.capability,
        target=contract.target,
        resource=contract.resource,
        parameters=dict(contract.parameters),
        user_requested=contract.user_requested,
        confidence=contract.confidence,
    )


__all__ = [
    "artifact_contract_to_input",
    "capability_contract_to_request",
    "inbound_contract_to_envelope",
]
