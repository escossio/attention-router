from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
    CapabilityLabComparison,
    CapabilityLabObservation,
    CapabilityLabScenario,
    compare_scenario,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.platform.evidence import (
    EvidenceReferenceInput,
    EvidenceType,
    create_evidence_reference,
)
from attention_router.platform.operations import ObservationInput, record_observation


SCENARIO_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "web"
    / "static"
    / "capability-lab-scenarios.json"
)


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


class CapabilityLabProbeReport(BaseModel):
    """One read-only feature probe plus fail-closed acceptance comparison."""

    model_config = ConfigDict(extra="forbid")

    certification: Literal["EPHEMERAL_ONLY"] = "EPHEMERAL_ONLY"
    probe: CapabilityLabProbeResult
    comparison: CapabilityLabComparison


class CapabilityLabT0EvidenceReport(BaseModel):
    """Durable T0 evidence over the same canonical resolver; never execution authority."""

    model_config = ConfigDict(extra="forbid")

    certification: Literal["DURABLE_T0_ONLY"] = "DURABLE_T0_ONLY"
    probe: CapabilityLabProbeResult
    operational_observation_id: str
    evidence_reference_id: str
    observation: CapabilityLabObservation
    comparison: CapabilityLabComparison


def load_capability_lab_scenarios() -> dict[str, CapabilityLabScenario]:
    """Load the repository-owned synthetic hypotheses used by the Lab.

    The caller can select only a scenario id from this fixed fixture. Capability,
    requester identity and expected authority cannot be supplied by an HTTP client.
    """

    payload = json.loads(SCENARIO_FIXTURE.read_text(encoding="utf-8"))
    scenarios = [CapabilityLabScenario.model_validate(item) for item in payload]
    by_id = {scenario.scenario_id: scenario for scenario in scenarios}
    if len(by_id) != len(scenarios):
        raise ValueError("CAPABILITY_LAB_SCENARIO_IDS_MUST_BE_UNIQUE")
    return by_id


def capability_lab_scenario(scenario_id: str) -> CapabilityLabScenario:
    scenarios = load_capability_lab_scenarios()
    scenario = scenarios.get(scenario_id)
    if scenario is None:
        raise KeyError("CAPABILITY_LAB_SCENARIO_UNKNOWN")
    return scenario


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


def probe_named_scenario_t0(
    session: Session,
    scenario_id: str,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> CapabilityLabProbeReport:
    """Probe only a repository-owned synthetic scenario, never client-defined authority."""

    scenario = capability_lab_scenario(scenario_id)
    probe = probe_capability_t0(
        session,
        scenario,
        tenant_id=tenant_id,
        policy_allows=True,
    )
    return CapabilityLabProbeReport(
        probe=probe,
        comparison=compare_scenario(scenario, probe.observation),
    )


def record_capability_t0_evidence(
    session: Session,
    scenario: CapabilityLabScenario,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    source_sha: str,
    runtime_sha: str,
    schema_revision: str,
    now: datetime | None = None,
    freshness_seconds: int = 300,
) -> CapabilityLabT0EvidenceReport:
    """Persist minimal synthetic T0 evidence without granting or executing anything.

    The canonical resolver remains the source of the semantic result. This function
    records only a sanitized operational observation plus an EvidenceReference that
    points to it. It never creates a grant, human authorization, ScenarioRun, outbox
    item, transport call or external effect. Transaction commit remains caller-owned.
    """

    if not source_sha or not runtime_sha or not schema_revision:
        raise ValueError("CAPABILITY_LAB_T0_PROVENANCE_REQUIRED")
    if freshness_seconds < 1 or freshness_seconds > 3600:
        raise ValueError("CAPABILITY_LAB_T0_FRESHNESS_INVALID")

    timestamp = now or datetime.now(UTC)
    probe = probe_capability_t0(
        session,
        scenario,
        tenant_id=tenant_id,
        policy_allows=True,
    )

    operational = record_observation(
        session,
        ObservationInput(
            tenant_id=tenant_id,
            source="capability_lab_t0_semantic_evaluator",
            source_type="CAPABILITY_RESOLUTION",
            status=probe.resolution_status.value,
            reason_code=probe.reason_code,
            observed_at=timestamp,
            received_at=timestamp,
            freshness_expires_at=timestamp + timedelta(seconds=freshness_seconds),
            component_key="capability_lab.t0",
            lineage_classification="SYNTHETIC",
            source_revision=source_sha,
            runtime_revision=runtime_sha,
            schema_revision=schema_revision,
            metadata={
                "stage": "T0",
                "scenario_id": scenario.scenario_id,
                "capability": scenario.capability_key,
                "resolution_status": probe.resolution_status.value,
                "authority_result": probe.authority_result.value,
                "approval_required": probe.approval_required,
                "execution_allowed": probe.execution_allowed,
                "synthetic": True,
                "production_effects": False,
            },
        ),
        now=timestamp,
    )

    evidence = create_evidence_reference(
        session,
        EvidenceReferenceInput(
            tenant_id=tenant_id,
            evidence_type=EvidenceType.API_RESULT,
            internal_entity_type="operational_observation",
            internal_entity_id=operational.id,
            source_sha=source_sha,
            metadata={
                "stage": "T0",
                "scenario_id": scenario.scenario_id,
                "capability": scenario.capability_key,
                "resolution_status": probe.resolution_status.value,
                "authority_result": probe.authority_result.value,
                "lineage": "SYNTHETIC",
            },
        ),
        now=timestamp,
    )

    observation = CapabilityLabObservation(
        scenario_id=scenario.scenario_id,
        observed_resolution=probe.authority_result,
        observed_reason_code=probe.reason_code,
        resolution_evidence_refs=[evidence.id],
    )
    return CapabilityLabT0EvidenceReport(
        probe=probe,
        operational_observation_id=operational.id,
        evidence_reference_id=evidence.id,
        observation=observation,
        comparison=compare_scenario(scenario, observation),
    )
