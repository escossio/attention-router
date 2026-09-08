from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import (
    DependencyDefinitionRow,
    OperationalObservationRow,
    ReadinessResultRow,
    TenantRow,
)
from attention_router.platform.operations import (
    DependencyCriticality,
    DependencyDefinitionInput,
    DependencyType,
    ObservationInput,
    link_dependency,
    persist_readiness_result,
    record_observation,
    upsert_dependency,
)
from attention_router.platform.privacy import (
    PrivacyViolation,
    SanitizationMode,
    sanitize_metadata,
)
from attention_router.platform.readiness import (
    DependencyRelation,
    DependencySignal,
    ReadinessDenied,
    ReadinessState,
    evaluate_readiness_bundle,
    require_dispatch_readiness,
)


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _dependency_input(key: str, *, tenant_id: str = DEFAULT_TENANT_ID):
    return DependencyDefinitionInput(
        tenant_id=tenant_id,
        canonical_key=key,
        dependency_type=DependencyType.PROCESS,
        owner_module="platform.operations",
        criticality=DependencyCriticality.HIGH,
        authoritative_source="compose",
        health_source="health.ready",
        freshness_source="operational_observation",
        provenance_version=1,
        source_revision="candidate-sha",
        metadata={"endpoint_class": "internal"},
    )


def _signal(
    dependency_id: str,
    state: ReadinessState,
    *,
    relation: DependencyRelation = DependencyRelation.MANDATORY,
    evidence_only: bool = False,
    fresh: bool = True,
) -> DependencySignal:
    return DependencySignal(
        dependency_id=dependency_id,
        state=state,
        relation=relation,
        observed_at=NOW - timedelta(seconds=2),
        freshness_expires_at=NOW + timedelta(seconds=20) if fresh else NOW - timedelta(seconds=1),
        reason_code=f"{dependency_id}_{state.value}",
        evidence_only=evidence_only,
    )


def test_privacy_rejects_sensitive_fields_and_can_redact() -> None:
    with pytest.raises(PrivacyViolation, match="FORBIDDEN_EVIDENCE_FIELD"):
        sanitize_metadata({"api_key_sentinel": "fixture"})

    result = sanitize_metadata(
        {"safe": "value", "raw_message": "synthetic fixture"},
        mode=SanitizationMode.REDACT,
    )

    assert result.value == {"safe": "value", "raw_message": "[REDACTED]"}
    assert result.redacted_paths == ("raw_message",)


def test_readiness_keeps_evidence_outage_separate_from_domain() -> None:
    bundle = evaluate_readiness_bundle(
        subject_type="CAPABILITY",
        subject_key="message.response",
        signals=(
            _signal("postgres", ReadinessState.READY),
            _signal(
                "tempo",
                ReadinessState.BLOCKED,
                relation=DependencyRelation.ADVISORY,
                evidence_only=True,
            ),
        ),
        now=NOW,
    )

    assert bundle.domain.state is ReadinessState.READY
    assert bundle.evidence.state is ReadinessState.DEGRADED
    require_dispatch_readiness(bundle, now=NOW)
    with pytest.raises(ReadinessDenied, match="EVIDENCE_READINESS_DEGRADED_DENIES"):
        require_dispatch_readiness(bundle, evidence_required=True, now=NOW)


def test_stale_mandatory_dependency_denies_at_dispatch() -> None:
    bundle = evaluate_readiness_bundle(
        subject_type="SCENARIO",
        subject_key="bounded-synthetic",
        signals=(_signal("transport", ReadinessState.READY, fresh=False),),
        now=NOW,
    )

    assert bundle.domain.state is ReadinessState.STALE
    with pytest.raises(ReadinessDenied, match="DOMAIN_READINESS_STALE_DENIES"):
        require_dispatch_readiness(bundle, now=NOW)


def test_dependency_registry_is_tenant_scoped(session) -> None:
    upstream = upsert_dependency(session, _dependency_input("database"), now=NOW)
    downstream = upsert_dependency(session, _dependency_input("api"), now=NOW)
    edge = link_dependency(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        upstream_dependency_id=upstream.id,
        downstream_dependency_id=downstream.id,
        relation=DependencyRelation.MANDATORY,
        provenance="test:v1",
        now=NOW,
    )
    assert edge.relation_type == "MANDATORY"

    tenant = TenantRow(
        id="tenant-other",
        slug="tenant-other",
        name="Other",
        status="ACTIVE",
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(tenant)
    other = upsert_dependency(session, _dependency_input("other", tenant_id=tenant.id), now=NOW)
    with pytest.raises(ValueError, match="DEPENDENCY_CROSS_TENANT_OR_UNKNOWN"):
        link_dependency(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            upstream_dependency_id=upstream.id,
            downstream_dependency_id=other.id,
            relation=DependencyRelation.MANDATORY,
            provenance="test:v1",
            now=NOW,
        )


def test_observation_and_current_readiness_are_sanitized_and_superseded(session) -> None:
    dependency = upsert_dependency(session, _dependency_input("database"), now=NOW)
    observation = record_observation(
        session,
        ObservationInput(
            tenant_id=DEFAULT_TENANT_ID,
            source="database.health",
            source_type="HEALTH",
            status="READY",
            reason_code="DATABASE_READY",
            observed_at=NOW,
            received_at=NOW,
            freshness_expires_at=NOW + timedelta(seconds=30),
            dependency_id=dependency.id,
            metadata={"latency_class": "bounded"},
        ),
        now=NOW,
    )
    assert observation.sanitized_metadata == {"latency_class": "bounded"}

    first = evaluate_readiness_bundle(
        subject_type="COMPONENT",
        subject_key="database",
        signals=(_signal(dependency.id, ReadinessState.READY),),
        now=NOW,
    ).component
    first_row = persist_readiness_result(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        evaluation=first,
        source_references=(observation.id,),
        provenance={"runtime_revision": "candidate-sha"},
    )
    second = evaluate_readiness_bundle(
        subject_type="COMPONENT",
        subject_key="database",
        signals=(_signal(dependency.id, ReadinessState.BLOCKED),),
        now=NOW + timedelta(seconds=1),
    ).component
    second_row = persist_readiness_result(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        evaluation=second,
        source_references=(observation.id,),
        provenance={"runtime_revision": "candidate-sha"},
    )

    assert first_row.is_current is False
    assert second_row.is_current is True
    assert second_row.state == "BLOCKED"
    assert len(session.scalars(select(ReadinessResultRow)).all()) == 2
    assert session.scalar(select(OperationalObservationRow)).id == observation.id
    assert len(session.scalars(select(DependencyDefinitionRow)).all()) == 1
