from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from attention_router.application.platform.registry import resolve_capability_request
from attention_router.core.capabilities import CapabilityRequest, CapabilityResolutionStatus
from attention_router.core.providers import ProviderResult, ProviderRuntimeRegistry
from attention_router.infrastructure.models import AgentExecutionIntentRow
from attention_router.observability.tracing import safe_set_attribute, set_outcome, start_span


@dataclass(frozen=True)
class CapabilityExecutionOutcome:
    capability: str
    status: str
    reason_code: str
    provider_instance_id: str | None = None
    result: dict | None = None


def attach_capability_to_execution_intent(
    intent: AgentExecutionIntentRow,
    request: CapabilityRequest,
    *,
    provider_instance_id: str | None,
    canonical_event_id: str | None,
) -> AgentExecutionIntentRow:
    """Reuse the existing intent ledger without making capability execution automatic."""
    intent.capability_name = request.capability
    intent.capability_request = request.model_dump(mode="json")
    intent.provider_instance_id = provider_instance_id
    intent.canonical_event_id = canonical_event_id
    return intent


def execute_capability(
    session: Session,
    request: CapabilityRequest,
    *,
    tenant_id: str,
    grantee_type: str,
    grantee_id: str,
    policy_allows: bool,
    runtime_registry: ProviderRuntimeRegistry,
    resource_id: str | None = None,
    approval_granted: bool = False,
    owner_authorized: bool = False,
) -> CapabilityExecutionOutcome:
    """Generic provider invocation; Andy never calls this function directly."""
    resolution = resolve_capability_request(
        session,
        request,
        tenant_id=tenant_id,
        grantee_type=grantee_type,
        grantee_id=grantee_id,
        policy_allows=policy_allows,
        resource_id=resource_id,
        owner_authorized=owner_authorized,
    )
    if resolution.status in {
        CapabilityResolutionStatus.UNKNOWN,
        CapabilityResolutionStatus.KNOWN_BUT_UNAVAILABLE,
        CapabilityResolutionStatus.AVAILABLE_NOT_AUTHORIZED,
    }:
        return CapabilityExecutionOutcome(
            request.capability,
            "BLOCKED",
            resolution.reason_code,
            resolution.provider_instance_id,
        )
    if resolution.approval_required and not approval_granted:
        return CapabilityExecutionOutcome(
            request.capability,
            "REQUIRES_APPROVAL",
            "APPROVAL_REQUIRED",
            resolution.provider_instance_id,
        )
    if not resolution.provider_instance_id or not resolution.provider_interface:
        return CapabilityExecutionOutcome(
            request.capability,
            "AUTHORIZED",
            "INTERNAL_RUNTIME_REQUIRED",
        )
    provider = runtime_registry.resolve(
        resolution.provider_instance_id,
        resolution.provider_interface,
    )
    if provider is None:
        return CapabilityExecutionOutcome(
            request.capability,
            "BLOCKED",
            "PROVIDER_RUNTIME_UNAVAILABLE",
            resolution.provider_instance_id,
        )
    with start_span("capability.execute") as span:
        safe_set_attribute(span, "attention.tenant_id", tenant_id)
        safe_set_attribute(span, "attention.capability", request.capability)
        safe_set_attribute(span, "attention.provider_interface", resolution.provider_interface)
        result: ProviderResult = provider.execute(request.capability, request.parameters)
        safe_set_attribute(span, "attention.execution_result", "SUCCESS" if result.success else "FAILED")
        safe_set_attribute(span, "attention.reason_code", result.reason_code)
        set_outcome(span, "EXECUTED" if result.success else "FAILED", error=not result.success)
        return CapabilityExecutionOutcome(
            request.capability,
            "EXECUTED" if result.success else "FAILED",
            result.reason_code,
            resolution.provider_instance_id,
            result.result,
        )
