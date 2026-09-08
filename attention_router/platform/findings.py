from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from attention_router.platform.evidence import EvidenceReferenceInput, create_evidence_reference
from attention_router.platform.privacy import sanitize_metadata, sanitized_summary


class FindingCategory(StrEnum):
    FAILURE = "FAILURE"
    BLOCKER = "BLOCKER"
    ANOMALY = "ANOMALY"
    ARCHITECTURAL_GAP = "ARCHITECTURAL_GAP"


class FindingSeverity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class FindingStatus(StrEnum):
    NEW = "NEW"
    ACTIVE = "ACTIVE"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    SUPPRESSED = "SUPPRESSED"
    EXPECTED = "EXPECTED"


class FindingInputClass(StrEnum):
    UNEXPECTED = "UNEXPECTED"
    EXPECTED_DENY = "EXPECTED_DENY"


class FindingAction(StrEnum):
    CREATED = "CREATED"
    AGGREGATED = "AGGREGATED"
    REOPENED = "REOPENED"
    EXPECTED_DENY_ONLY = "EXPECTED_DENY_ONLY"


@dataclass(frozen=True, slots=True)
class FindingCandidate:
    tenant_id: str
    category: FindingCategory
    severity: FindingSeverity
    title: str
    summary: str
    component_key: str
    reason_code: str
    fingerprint_version: int = 1
    active_scope: str = "CURRENT"
    normalized_scope: dict[str, Any] = field(default_factory=dict)
    collision_discriminator: str | None = None
    lineage_classification: str = "HISTORICAL_UNKNOWN"
    scenario_run_id: str | None = None
    correlation_id: str | None = None
    provenance: str = "platform.findings:v1"
    input_class: FindingInputClass = FindingInputClass.UNEXPECTED
    evidence: tuple[EvidenceReferenceInput, ...] = ()

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.component_key or not self.reason_code:
            raise ValueError("FINDING_SCOPE_REQUIRED")
        if self.fingerprint_version < 1:
            raise ValueError("FINDING_FINGERPRINT_VERSION_INVALID")
        sanitized_summary(self.title, maximum_length=240)
        sanitized_summary(self.summary, maximum_length=1000)
        sanitize_metadata(self.normalized_scope)
        if any(evidence.tenant_id != self.tenant_id for evidence in self.evidence):
            raise ValueError("CROSS_TENANT_EVIDENCE_REFERENCE")


@dataclass(frozen=True, slots=True)
class FindingRecordResult:
    action: FindingAction
    finding_id: str | None
    occurrence_id: str | None
    fingerprint: str | None


def finding_fingerprint(candidate: FindingCandidate) -> str:
    material = {
        "tenant_id": candidate.tenant_id,
        "fingerprint_version": candidate.fingerprint_version,
        "category": candidate.category.value,
        "component_key": candidate.component_key,
        "reason_code": candidate.reason_code,
        "scope": sanitize_metadata(candidate.normalized_scope).value,
        "collision_discriminator": candidate.collision_discriminator,
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def record_finding(
    session: Session,
    candidate: FindingCandidate,
    *,
    observed_at: datetime | None = None,
) -> FindingRecordResult:
    """Atomically upsert current state and append an immutable occurrence."""

    from attention_router.infrastructure.models import FindingOccurrenceRow, FindingRow

    if candidate.input_class is FindingInputClass.EXPECTED_DENY:
        return FindingRecordResult(FindingAction.EXPECTED_DENY_ONLY, None, None, None)

    timestamp = observed_at or datetime.now(UTC)
    fingerprint = finding_fingerprint(candidate)
    finding = _lock_current_finding(
        session,
        tenant_id=candidate.tenant_id,
        fingerprint_version=candidate.fingerprint_version,
        fingerprint=fingerprint,
        active_scope=candidate.active_scope,
    )
    action = FindingAction.AGGREGATED
    if finding is None:
        finding = FindingRow(
            id=str(uuid4()),
            tenant_id=candidate.tenant_id,
            fingerprint=fingerprint,
            fingerprint_version=candidate.fingerprint_version,
            active_scope=candidate.active_scope,
            category=candidate.category.value,
            severity=candidate.severity.value,
            status=FindingStatus.NEW.value,
            title=sanitized_summary(candidate.title, maximum_length=240),
            summary=sanitized_summary(candidate.summary, maximum_length=1000),
            first_seen_at=timestamp,
            last_seen_at=timestamp,
            occurrence_count=1,
            lineage_classification=candidate.lineage_classification,
            owner_reference=None,
            acknowledged_by=None,
            acknowledged_at=None,
            suppression_scope=None,
            suppression_reason=None,
            resolved_at=None,
            resolution_reason=None,
            provenance={"source": candidate.provenance},
            version=1,
            created_at=timestamp,
            updated_at=timestamp,
        )
        try:
            with session.begin_nested():
                session.add(finding)
                session.flush()
            action = FindingAction.CREATED
        except IntegrityError:
            finding = _lock_current_finding(
                session,
                tenant_id=candidate.tenant_id,
                fingerprint_version=candidate.fingerprint_version,
                fingerprint=fingerprint,
                active_scope=candidate.active_scope,
            )
            if finding is None:
                raise
    elif finding.status == FindingStatus.RESOLVED.value:
        finding.status = FindingStatus.ACTIVE.value
        finding.resolved_at = None
        finding.resolution_reason = None
        finding.version += 1
        action = FindingAction.REOPENED

    occurrence = FindingOccurrenceRow(
        id=str(uuid4()),
        tenant_id=candidate.tenant_id,
        finding_id=finding.id,
        observed_at=timestamp,
        component_key=candidate.component_key,
        scenario_run_id=candidate.scenario_run_id,
        correlation_id=candidate.correlation_id,
        reason_code=candidate.reason_code,
        provenance={"source": candidate.provenance},
        created_at=timestamp,
    )
    session.add(occurrence)
    finding.last_seen_at = timestamp
    if action is not FindingAction.CREATED:
        finding.occurrence_count += 1
    finding.updated_at = timestamp
    if finding.status == FindingStatus.NEW.value and finding.occurrence_count > 1:
        finding.status = FindingStatus.ACTIVE.value
    session.flush()
    for evidence in candidate.evidence:
        create_evidence_reference(
            session,
            replace(evidence, finding_id=finding.id, finding_occurrence_id=occurrence.id),
            now=timestamp,
        )
    return FindingRecordResult(action, finding.id, occurrence.id, fingerprint)


def _lock_current_finding(
    session: Session,
    *,
    tenant_id: str,
    fingerprint_version: int,
    fingerprint: str,
    active_scope: str,
) -> Any | None:
    from attention_router.infrastructure.models import FindingRow

    return session.scalar(
        select(FindingRow)
        .where(
            FindingRow.tenant_id == tenant_id,
            FindingRow.fingerprint_version == fingerprint_version,
            FindingRow.fingerprint == fingerprint,
            FindingRow.active_scope == active_scope,
        )
        .order_by(FindingRow.created_at.desc())
        .with_for_update()
    )


def acknowledge_finding(
    session: Session,
    *,
    tenant_id: str,
    finding_id: str,
    acknowledged_by: str,
    now: datetime | None = None,
) -> Any:
    finding = _tenant_finding(session, tenant_id=tenant_id, finding_id=finding_id)
    if finding.status in {FindingStatus.RESOLVED.value, FindingStatus.SUPPRESSED.value}:
        raise ValueError("FINDING_STATUS_CANNOT_ACKNOWLEDGE")
    timestamp = now or datetime.now(UTC)
    finding.status = FindingStatus.ACKNOWLEDGED.value
    finding.acknowledged_by = acknowledged_by
    finding.acknowledged_at = timestamp
    finding.updated_at = timestamp
    finding.version += 1
    session.flush()
    return finding


def suppress_finding(
    session: Session,
    *,
    tenant_id: str,
    finding_id: str,
    scope: str,
    reason: str,
    now: datetime | None = None,
) -> Any:
    if not scope or not reason:
        raise ValueError("FINDING_SUPPRESSION_SCOPE_AND_REASON_REQUIRED")
    finding = _tenant_finding(session, tenant_id=tenant_id, finding_id=finding_id)
    timestamp = now or datetime.now(UTC)
    finding.status = FindingStatus.SUPPRESSED.value
    finding.suppression_scope = sanitized_summary(scope, maximum_length=240)
    finding.suppression_reason = sanitized_summary(reason, maximum_length=320)
    finding.updated_at = timestamp
    finding.version += 1
    session.flush()
    return finding


def resolve_finding(
    session: Session,
    *,
    tenant_id: str,
    finding_id: str,
    reason: str,
    now: datetime | None = None,
) -> Any:
    if not reason:
        raise ValueError("FINDING_RESOLUTION_REASON_REQUIRED")
    finding = _tenant_finding(session, tenant_id=tenant_id, finding_id=finding_id)
    timestamp = now or datetime.now(UTC)
    finding.status = FindingStatus.RESOLVED.value
    finding.resolved_at = timestamp
    finding.resolution_reason = sanitized_summary(reason, maximum_length=320)
    finding.updated_at = timestamp
    finding.version += 1
    session.flush()
    return finding


def list_findings(
    session: Session,
    *,
    tenant_id: str,
    statuses: Iterable[FindingStatus] | None = None,
) -> list[dict[str, Any]]:
    from attention_router.infrastructure.models import FindingRow

    statement = select(FindingRow).where(FindingRow.tenant_id == tenant_id)
    if statuses is not None:
        statement = statement.where(FindingRow.status.in_(tuple(status.value for status in statuses)))
    rows = session.scalars(statement.order_by(FindingRow.last_seen_at.desc())).all()
    return [finding_to_dict(row) for row in rows]


def finding_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "fingerprint": row.fingerprint,
        "fingerprint_version": row.fingerprint_version,
        "category": row.category,
        "severity": row.severity,
        "status": row.status,
        "title": row.title,
        "summary": row.summary,
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
        "occurrence_count": row.occurrence_count,
        "lineage_classification": row.lineage_classification,
        "acknowledged_at": row.acknowledged_at,
        "resolved_at": row.resolved_at,
        "version": row.version,
    }


def _tenant_finding(session: Session, *, tenant_id: str, finding_id: str) -> Any:
    from attention_router.infrastructure.models import FindingRow

    finding = session.scalar(
        select(FindingRow)
        .where(FindingRow.id == finding_id, FindingRow.tenant_id == tenant_id)
        .with_for_update()
    )
    if finding is None:
        raise ValueError("FINDING_NOT_FOUND_OR_CROSS_TENANT")
    return finding
