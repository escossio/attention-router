from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from attention_router.application.platform.registry import resolve_capability_request
from attention_router.core.authority import AuthorityResult
from attention_router.core.capabilities import (
    CapabilityRequest,
    CapabilityResolutionStatus,
)
from attention_router.core.capability_lab import (
    CapabilityLabObservation,
    CapabilityLabScenario,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID


class CapabilityLabProbeResult(BaseModel):
    """Ephemeral T0 observation from the canonical capability runtime."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    capability_key: str
    resolution_status: CapabilityResolutionStatus
    authority_result: AuthorityResult
    reason_code: str
    provider_interface: str | None = None
    approval_required: bool
    execution_allowed: bool
    durable_evidence: Literal[False] = False
    observation: CapabilityLabObservation


def probe_capability_t0(
    session: Session,
    scenario: CapabilityLabScenario,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    policy_allows: bool = True,
) -> CapabilityLabProbeResult:
    """Observe canonical capability resolution without creating authority or evidence.

    The probe deliberately supplies no durable evidence reference. Even when the
    observed authority matches the hypothesis, certification therefore remains
    INCOMPLETE until a real evidence-producing semantic scenario is bound.
    """

    request = CapabilityRequest(
        capability=scenario.capability_key,
        parameters={},
        user_requested=True,
        confidence="high",
    )
    resolution = resolve_capability_request(
        session,
        request,
        tenant_id=tenant_id,
        grantee_type="ACTOR",
        grantee_id=scenario.requester_actor_key,
        policy_allows=policy_allows,
        owner_authorized=False,
    )
    authority = AuthorityResult(resolution.authority_result)
    observation = CapabilityLabObservation(
        scenario_id=scenario.scenario_id,
        observed_resolution=authority,
        observed_reason_code=resolution.reason_code,
    )
    return CapabilityLabProbeResult(
        scenario_id=scenario.scenario_id,
        capability_key=scenario.capability_key,
        resolution_status=resolution.status,
        authority_result=authority,
        reason_code=resolution.reason_code,
        provider_interface=resolution.provider_interface,
        approval_required=resolution.approval_required,
        execution_allowed=resolution.execution_allowed,
        observation=observation,
    )
