from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Iterable, Mapping
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from attention_router.platform.privacy import sanitize_metadata
from attention_router.platform.readiness import (
    DependencyRelation,
    ReadinessEvaluation,
)


class DependencyType(StrEnum):
    MODULE = "MODULE"
    PROCESS = "PROCESS"
    PROVIDER = "PROVIDER"
    PERSISTENCE = "PERSISTENCE"
    TRANSPORT = "TRANSPORT"
    API = "API"
    HEALTH_SOURCE = "HEALTH_SOURCE"
    PROMOTION_DEPENDENCY = "PROMOTION_DEPENDENCY"


class DependencyCriticality(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass(frozen=True, slots=True)
class DependencyDefinitionInput:
    tenant_id: str
    canonical_key: str
    dependency_type: DependencyType
    owner_module: str
    criticality: DependencyCriticality
    authoritative_source: str
    health_source: str
    freshness_source: str
    provenance_version: int
    source_revision: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.canonical_key or not self.owner_module:
            raise ValueError("DEPENDENCY_IDENTITY_REQUIRED")
        if self.provenance_version < 1:
            raise ValueError("DEPENDENCY_PROVENANCE_VERSION_INVALID")
        sanitize_metadata(self.metadata)


@dataclass(frozen=True, slots=True)
class ObservationInput:
    tenant_id: str
    source: str
    source_type: str
    status: str
    reason_code: str
    observed_at: datetime
    received_at: datetime
    freshness_expires_at: datetime
    dependency_id: str | None = None
    component_key: str | None = None
    lineage_classification: str = "HISTORICAL_UNKNOWN"
    scenario_run_id: str | None = None
    correlation_id: str | None = None
    source_revision: str | None = None
    runtime_revision: str | None = None
    schema_revision: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.source or not self.source_type:
            raise ValueError("OBSERVATION_SOURCE_REQUIRED")
        if not self.dependency_id and not self.component_key:
            raise ValueError("OBSERVATION_SUBJECT_REQUIRED")
        observed = _utc(self.observed_at)
        received = _utc(self.received_at)
        if received < observed:
            raise ValueError("OBSERVATION_RECEIVED_BEFORE_OBSERVED")
        if _utc(self.freshness_expires_at) < observed:
            raise ValueError("OBSERVATION_FRESHNESS_INVALID")
        sanitize_metadata(self.metadata)


@dataclass(frozen=True, slots=True)
class RuntimeProvenance:
    source_revision: str
    runtime_revision: str
    schema_revision: str
    observed_at: datetime
    fresh_until: datetime

    @property
    def source_runtime_match(self) -> bool:
        return self.source_revision == self.runtime_revision


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def upsert_dependency(
    session: Session,
    request: DependencyDefinitionInput,
    *,
    now: datetime | None = None,
) -> Any:
    from attention_router.infrastructure.models import DependencyDefinitionRow

    timestamp = now or datetime.now(UTC)
    row = session.scalar(
        select(DependencyDefinitionRow).where(
            DependencyDefinitionRow.tenant_id == request.tenant_id,
            DependencyDefinitionRow.canonical_key == request.canonical_key,
        )
    )
    values = {
        "dependency_type": request.dependency_type.value,
        "owner_module": request.owner_module,
        "criticality": request.criticality.value,
        "authoritative_source": request.authoritative_source,
        "health_source": request.health_source,
        "freshness_source": request.freshness_source,
        "sanitized_metadata": sanitize_metadata(request.metadata).value,
        "is_active": True,
        "provenance_version": request.provenance_version,
        "source_revision": request.source_revision,
        "updated_at": timestamp,
    }
    if row is None:
        row = DependencyDefinitionRow(
            id=str(uuid4()),
            tenant_id=request.tenant_id,
            canonical_key=request.canonical_key,
            created_at=timestamp,
            **values,
        )
        session.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    session.flush()
    return row


def link_dependency(
    session: Session,
    *,
    tenant_id: str,
    upstream_dependency_id: str,
    downstream_dependency_id: str,
    relation: DependencyRelation,
    provenance: str | dict[str, Any],
    now: datetime | None = None,
) -> Any:
    from attention_router.infrastructure.models import DependencyDefinitionRow, DependencyEdgeRow

    if upstream_dependency_id == downstream_dependency_id:
        raise ValueError("DEPENDENCY_SELF_EDGE_FORBIDDEN")
    dependency_ids = set(
        session.scalars(
            select(DependencyDefinitionRow.id).where(
                DependencyDefinitionRow.tenant_id == tenant_id,
                DependencyDefinitionRow.id.in_((upstream_dependency_id, downstream_dependency_id)),
            )
        )
    )
    if dependency_ids != {upstream_dependency_id, downstream_dependency_id}:
        raise ValueError("DEPENDENCY_CROSS_TENANT_OR_UNKNOWN")
    timestamp = now or datetime.now(UTC)
    row = session.scalar(
        select(DependencyEdgeRow).where(
            DependencyEdgeRow.tenant_id == tenant_id,
            DependencyEdgeRow.upstream_dependency_id == upstream_dependency_id,
            DependencyEdgeRow.downstream_dependency_id == downstream_dependency_id,
            DependencyEdgeRow.relation_type == relation.value,
        )
    )
    if row is None:
        row = DependencyEdgeRow(
            id=str(uuid4()),
            tenant_id=tenant_id,
            upstream_dependency_id=upstream_dependency_id,
            downstream_dependency_id=downstream_dependency_id,
            relation_type=relation.value,
            is_active=True,
            provenance=(
                sanitize_metadata(provenance).value
                if isinstance(provenance, dict)
                else {"source": provenance}
            ),
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(row)
    else:
        row.is_active = True
        row.provenance = (
            sanitize_metadata(provenance).value
            if isinstance(provenance, dict)
            else {"source": provenance}
        )
        row.updated_at = timestamp
    session.flush()
    return row


def record_observation(
    session: Session,
    request: ObservationInput,
    *,
    now: datetime | None = None,
) -> Any:
    from attention_router.infrastructure.models import OperationalObservationRow

    row = OperationalObservationRow(
        id=str(uuid4()),
        tenant_id=request.tenant_id,
        source=request.source,
        source_type=request.source_type,
        observed_at=request.observed_at,
        received_at=request.received_at,
        freshness_expires_at=request.freshness_expires_at,
        dependency_id=request.dependency_id,
        component_key=request.component_key,
        lineage_classification=request.lineage_classification,
        scenario_run_id=request.scenario_run_id,
        correlation_id=request.correlation_id,
        reason_code=request.reason_code,
        status=request.status,
        sanitized_metadata=sanitize_metadata(request.metadata).value,
        source_revision=request.source_revision,
        runtime_revision=request.runtime_revision,
        schema_revision=request.schema_revision,
        created_at=now or datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


def persist_readiness_result(
    session: Session,
    *,
    tenant_id: str,
    evaluation: ReadinessEvaluation,
    source_references: Iterable[str],
    provenance: dict[str, Any],
) -> Any:
    from attention_router.infrastructure.models import ReadinessResultRow

    session.execute(
        update(ReadinessResultRow)
        .where(
            ReadinessResultRow.tenant_id == tenant_id,
            ReadinessResultRow.dimension == evaluation.dimension.value,
            ReadinessResultRow.subject_type == evaluation.subject_type,
            ReadinessResultRow.subject_key == evaluation.subject_key,
            ReadinessResultRow.is_current.is_(True),
        )
        .values(is_current=False, superseded_at=evaluation.evaluated_at)
    )
    row = ReadinessResultRow(
        id=str(uuid4()),
        tenant_id=tenant_id,
        dimension=evaluation.dimension.value,
        subject_type=evaluation.subject_type,
        subject_key=evaluation.subject_key,
        state=evaluation.state.value,
        reason_codes=list(evaluation.reason_codes),
        evaluated_at=evaluation.evaluated_at,
        evidence_fresh_until=evaluation.evidence_fresh_until,
        source_references=[{"reference": reference} for reference in source_references],
        blocker_references=[
            {"dependency_id": reference} for reference in evaluation.blocker_references
        ],
        required_dependency_ids=list(evaluation.required_dependency_ids),
        provenance=sanitize_metadata(provenance).value,
        is_current=True,
        superseded_at=None,
        created_at=evaluation.evaluated_at,
    )
    session.add(row)
    session.flush()
    return row


def platform_snapshot(
    session: Session,
    *,
    tenant_id: str,
    runtime_provenance: RuntimeProvenance,
    gate_states: Mapping[str, bool | None],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the read-only data-plane view from approved operational tables."""

    from attention_router.infrastructure.models import (
        DependencyDefinitionRow,
        FindingRow,
        ReadinessResultRow,
        ScenarioRunRow,
    )

    timestamp = _utc(now or datetime.now(UTC))
    dependencies = session.scalars(
        select(DependencyDefinitionRow).where(
            DependencyDefinitionRow.tenant_id == tenant_id,
            DependencyDefinitionRow.is_active.is_(True),
        )
    ).all()
    readiness = session.scalars(
        select(ReadinessResultRow).where(
            ReadinessResultRow.tenant_id == tenant_id,
            ReadinessResultRow.is_current.is_(True),
        )
    ).all()
    findings = session.scalars(
        select(FindingRow).where(
            FindingRow.tenant_id == tenant_id,
            FindingRow.status.in_(("NEW", "ACTIVE", "ACKNOWLEDGED")),
        )
    ).all()
    scenario_runs = session.scalars(
        select(ScenarioRunRow).where(ScenarioRunRow.tenant_id == tenant_id).limit(20)
    ).all()
    return {
        "tenant_id": tenant_id,
        "generated_at": timestamp,
        "stale": timestamp > _utc(runtime_provenance.fresh_until),
        "gates": {
            key: {
                "enabled": value,
                "state": "UNKNOWN" if value is None else ("OPEN" if value else "CLOSED"),
            }
            for key, value in sorted(gate_states.items())
        },
        "runtime_provenance": {
            "source_revision": runtime_provenance.source_revision,
            "runtime_revision": runtime_provenance.runtime_revision,
            "schema_revision": runtime_provenance.schema_revision,
            "observed_at": runtime_provenance.observed_at,
            "fresh_until": runtime_provenance.fresh_until,
            "source_runtime_match": runtime_provenance.source_runtime_match,
        },
        "dependencies": [_dependency_to_dict(row) for row in dependencies],
        "readiness": [_readiness_to_dict(row, timestamp=timestamp) for row in readiness],
        "findings": [_finding_summary(row) for row in findings],
        "scenario_runs": [_scenario_summary(row) for row in scenario_runs],
    }


def _dependency_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "canonical_key": row.canonical_key,
        "dependency_type": row.dependency_type,
        "owner_module": row.owner_module,
        "criticality": row.criticality,
        "authoritative_source": row.authoritative_source,
        "source_revision": row.source_revision,
        "provenance_version": row.provenance_version,
    }


def _readiness_to_dict(row: Any, *, timestamp: datetime) -> dict[str, Any]:
    stale = row.evidence_fresh_until is None or timestamp > _utc(row.evidence_fresh_until)
    return {
        "id": row.id,
        "dimension": row.dimension,
        "subject_type": row.subject_type,
        "subject_key": row.subject_key,
        "state": "STALE" if stale and row.state == "READY" else row.state,
        "reason_codes": list(row.reason_codes or []),
        "evaluated_at": row.evaluated_at,
        "evidence_fresh_until": row.evidence_fresh_until,
        "stale": stale,
    }


def _finding_summary(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "category": row.category,
        "severity": row.severity,
        "status": row.status,
        "title": row.title,
        "summary": row.summary,
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
        "occurrence_count": row.occurrence_count,
        "lineage_classification": row.lineage_classification,
    }


def _scenario_summary(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "scenario_version_id": row.scenario_version_id,
        "status": row.status,
        "root_correlation_id": row.root_correlation_id,
        "started_at": row.started_at,
        "terminal_at": row.completed_at,
    }


def list_dependency_records(session: Session, *, tenant_id: str) -> list[dict[str, Any]]:
    from attention_router.infrastructure.models import DependencyDefinitionRow

    rows = session.scalars(
        select(DependencyDefinitionRow)
        .where(DependencyDefinitionRow.tenant_id == tenant_id)
        .order_by(DependencyDefinitionRow.canonical_key)
    ).all()
    return [_dependency_to_dict(row) | {"is_active": row.is_active} for row in rows]


def list_current_readiness(session: Session, *, tenant_id: str) -> list[dict[str, Any]]:
    from attention_router.infrastructure.models import ReadinessResultRow

    timestamp = datetime.now(UTC)
    rows = session.scalars(
        select(ReadinessResultRow)
        .where(
            ReadinessResultRow.tenant_id == tenant_id,
            ReadinessResultRow.is_current.is_(True),
        )
        .order_by(ReadinessResultRow.dimension, ReadinessResultRow.subject_key)
    ).all()
    return [_readiness_to_dict(row, timestamp=timestamp) for row in rows]


def list_recent_observations(
    session: Session,
    *,
    tenant_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    from attention_router.infrastructure.models import OperationalObservationRow

    rows = session.scalars(
        select(OperationalObservationRow)
        .where(OperationalObservationRow.tenant_id == tenant_id)
        .order_by(OperationalObservationRow.received_at.desc())
        .limit(max(1, min(limit, 500)))
    ).all()
    return [
        {
            "id": row.id,
            "source": row.source,
            "source_type": row.source_type,
            "observed_at": row.observed_at,
            "received_at": row.received_at,
            "freshness_expires_at": row.freshness_expires_at,
            "dependency_id": row.dependency_id,
            "component_key": row.component_key,
            "lineage_classification": row.lineage_classification,
            "scenario_run_id": row.scenario_run_id,
            "correlation_id": row.correlation_id,
            "reason_code": row.reason_code,
            "status": row.status,
            "metadata": dict(row.sanitized_metadata or {}),
            "source_revision": row.source_revision,
            "runtime_revision": row.runtime_revision,
            "schema_revision": row.schema_revision,
        }
        for row in rows
    ]
