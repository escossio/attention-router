from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from attention_router.application.platform.registry import resolve_capability_request
from attention_router.core.authority import AuthorityResult
from attention_router.core.capabilities import CapabilityRequest, CapabilityResolutionStatus
from attention_router.core.capability_lab import (
    CapabilityLabT0Comparison,
    CapabilityLabT0Observation,
    CapabilityLabT0Scenario,
    compare_t0,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.platform.evidence import (
    EvidenceReferenceInput,
    EvidenceType,
    create_evidence_reference,
)
from attention_router.platform.operations import ObservationInput, record_observation


SCENARIO_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "platform"
    / "capability_lab_t0.v1.json"
)


class CapabilityLabT0ProbeReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    certification: Literal["EPHEMERAL_T0_ONLY"] = "EPHEMERAL_T0_ONLY"
    observation: CapabilityLabT0Observation
    comparison: CapabilityLabT0Comparison


class CapabilityLabT0EvidenceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    certification: Literal["DURABLE_T0_ONLY"] = "DURABLE_T0_ONLY"
    operational_observation_id: str
    evidence_reference_id: str
    observation: CapabilityLabT0Observation
    comparison: CapabilityLabT0Comparison


def load_capability_lab_t0_scenarios() -> dict[str, CapabilityLabT0Scenario]:
    payload = json.loads(SCENARIO_FIXTURE.read_text(encoding="utf-8"))
    scenarios = [CapabilityLabT0Scenario.model_validate(item) for item in payload]
    by_id = {scenario.scenario_id: scenario for scenario in scenarios}
    if len(by_id) != len(scenarios):
        raise ValueError("CAPABILITY_LAB_T0_SCENARIO_IDS_MUST_BE_UNIQUE")
    return by_id


def capability_lab_t0_scenario(scenario_id: str) -> CapabilityLabT0Scenario:
    scenario = load_capability_lab_t0_scenarios().get(scenario_id)
    if scenario is None:
        raise KeyError("CAPABILITY_LAB_T0_SCENARIO_UNKNOWN")
    return scenario


def observe_capability_t0(
    session: Session,
    scenario: CapabilityLabT0Scenario,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> CapabilityLabT0Observation:
    resolution = resolve_capability_request(
        session,
        CapabilityRequest(
            capability=scenario.capability_key,
            parameters={},
            user_requested=True,
            confidence="high",
        ),
        tenant_id=tenant_id,
        grantee_type="ACTOR",
        grantee_id=scenario.requester_actor_key,
        policy_allows=True,
        owner_authorized=False,
    )
    return CapabilityLabT0Observation(
        scenario_id=scenario.scenario_id,
        resolution_status=CapabilityResolutionStatus(resolution.status),
        authority_result=AuthorityResult(resolution.authority_result),
        reason_code=resolution.reason_code,
        provider_interface=resolution.provider_interface,
        approval_required=resolution.approval_required,
        execution_allowed=resolution.execution_allowed,
    )


def probe_named_capability_t0(
    session: Session,
    scenario_id: str,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> CapabilityLabT0ProbeReport:
    scenario = capability_lab_t0_scenario(scenario_id)
    observation = observe_capability_t0(session, scenario, tenant_id=tenant_id)
    return CapabilityLabT0ProbeReport(
        observation=observation,
        comparison=compare_t0(scenario, observation, require_evidence=False),
    )


def record_named_capability_t0_evidence(
    session: Session,
    scenario_id: str,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    source_sha: str,
    runtime_sha: str,
    schema_revision: str,
    now: datetime | None = None,
) -> CapabilityLabT0EvidenceReport:
    if not source_sha or not runtime_sha or not schema_revision:
        raise ValueError("CAPABILITY_LAB_T0_PROVENANCE_REQUIRED")

    scenario = capability_lab_t0_scenario(scenario_id)
    observation = observe_capability_t0(session, scenario, tenant_id=tenant_id)
    stamp = now or datetime.now(UTC)

    operational = record_observation(
        session,
        ObservationInput(
            tenant_id=tenant_id,
            source="capability_lab_t0",
            source_type="CAPABILITY_RESOLUTION",
            status=observation.resolution_status.value,
            reason_code=observation.reason_code,
            observed_at=stamp,
            received_at=stamp,
            freshness_expires_at=stamp + timedelta(minutes=5),
            component_key="capability_lab.t0",
            lineage_classification="SYNTHETIC",
            source_revision=source_sha,
            runtime_revision=runtime_sha,
            schema_revision=schema_revision,
            metadata={
                "scenario_id": scenario.scenario_id,
                "capability": scenario.capability_key,
                "authority_result": observation.authority_result.value,
                "provider_interface": observation.provider_interface,
                "synthetic": True,
                "production_effects": False,
            },
        ),
        now=stamp,
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
                "reason_code": observation.reason_code,
            },
        ),
        now=stamp,
    )
    durable = observation.model_copy(update={"evidence_refs": [evidence.id]})
    return CapabilityLabT0EvidenceReport(
        operational_observation_id=operational.id,
        evidence_reference_id=evidence.id,
        observation=durable,
        comparison=compare_t0(scenario, durable, require_evidence=True),
    )
