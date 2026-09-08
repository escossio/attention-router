from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from attention_router.platform.findings import (
    FindingCandidate,
    FindingCategory,
    FindingRecordResult,
    FindingSeverity,
    record_finding,
)
from attention_router.platform.operations import ObservationInput, record_observation
from attention_router.platform.privacy import sanitize_metadata


class DiscoveryKind(StrEnum):
    SOURCE_RUNTIME_DRIFT = "SOURCE_RUNTIME_DRIFT"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"
    TOPOLOGY_DRIFT = "TOPOLOGY_DRIFT"
    UNKNOWN_DEPENDENCY = "UNKNOWN_DEPENDENCY"
    NEW_REASON_SIGNATURE = "NEW_REASON_SIGNATURE"
    STALE_DEPENDENCY = "STALE_DEPENDENCY"


@dataclass(frozen=True, slots=True)
class DiscoveryProbe:
    tenant_id: str
    kind: DiscoveryKind
    component_key: str
    expected: str | None
    observed: str | None
    observed_at: datetime
    freshness_expires_at: datetime
    source: str = "platform.discovery"
    dependency_id: str | None = None
    source_revision: str | None = None
    runtime_revision: str | None = None
    schema_revision: str | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.component_key:
            raise ValueError("DISCOVERY_SCOPE_REQUIRED")
        sanitize_metadata(self.metadata)


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    drift_detected: bool
    reason_code: str
    observation: ObservationInput
    finding_candidate: FindingCandidate | None


@dataclass(frozen=True, slots=True)
class PersistedDiscoveryResult:
    observation_id: str
    finding: FindingRecordResult | None


class DiscoveryEngine:
    """Read-only reconciler: it emits evidence/finding candidates, never fixes state."""

    def evaluate(self, probe: DiscoveryProbe, *, now: datetime | None = None) -> DiscoveryResult:
        timestamp = _utc(now or datetime.now(UTC))
        stale = timestamp > _utc(probe.freshness_expires_at)
        drift = stale or probe.expected != probe.observed
        if stale:
            reason_code = "DISCOVERY_SOURCE_STALE"
        elif drift:
            reason_code = probe.kind.value
        else:
            reason_code = "DISCOVERY_MATCH"
        observation = ObservationInput(
            tenant_id=probe.tenant_id,
            source=probe.source,
            source_type="DISCOVERY",
            status="STALE" if stale else ("DRIFT" if drift else "MATCH"),
            reason_code=reason_code,
            observed_at=probe.observed_at,
            received_at=timestamp,
            freshness_expires_at=probe.freshness_expires_at,
            dependency_id=probe.dependency_id,
            component_key=probe.component_key,
            correlation_id=probe.correlation_id,
            source_revision=probe.source_revision,
            runtime_revision=probe.runtime_revision,
            schema_revision=probe.schema_revision,
            metadata={
                **sanitize_metadata(probe.metadata).value,
                "expected_fingerprint": probe.expected,
                "observed_fingerprint": probe.observed,
                "discovery_kind": probe.kind.value,
            },
        )
        finding = None
        if drift:
            category = (
                FindingCategory.BLOCKER
                if probe.kind in {DiscoveryKind.SOURCE_RUNTIME_DRIFT, DiscoveryKind.SCHEMA_DRIFT}
                else FindingCategory.ANOMALY
            )
            severity = (
                FindingSeverity.HIGH
                if category is FindingCategory.BLOCKER
                else FindingSeverity.MEDIUM
            )
            finding = FindingCandidate(
                tenant_id=probe.tenant_id,
                category=category,
                severity=severity,
                title=f"Operational drift: {probe.kind.value}",
                summary=f"{probe.component_key} differs from its authoritative operational source.",
                component_key=probe.component_key,
                reason_code=reason_code,
                normalized_scope={
                    "component_key": probe.component_key,
                    "dependency_id": probe.dependency_id,
                    "discovery_kind": probe.kind.value,
                },
                correlation_id=probe.correlation_id,
                provenance="platform.discovery:v1",
            )
        return DiscoveryResult(
            drift_detected=drift,
            reason_code=reason_code,
            observation=observation,
            finding_candidate=finding,
        )

    def persist(self, session: Session, result: DiscoveryResult) -> PersistedDiscoveryResult:
        observation = record_observation(session, result.observation)
        finding = (
            record_finding(session, result.finding_candidate)
            if result.finding_candidate is not None
            else None
        )
        return PersistedDiscoveryResult(observation_id=observation.id, finding=finding)


def stale_dependency_probe(
    *,
    tenant_id: str,
    dependency_id: str,
    component_key: str,
    last_observed_at: datetime,
    stale_after: timedelta,
    now: datetime | None = None,
) -> DiscoveryProbe:
    timestamp = now or datetime.now(UTC)
    return DiscoveryProbe(
        tenant_id=tenant_id,
        kind=DiscoveryKind.STALE_DEPENDENCY,
        component_key=component_key,
        expected="FRESH",
        observed="STALE" if timestamp > last_observed_at + stale_after else "FRESH",
        observed_at=last_observed_at,
        freshness_expires_at=last_observed_at + stale_after,
        dependency_id=dependency_id,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
