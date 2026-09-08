from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from attention_router.platform.privacy import sanitize_metadata, sanitized_summary


class EvidenceType(StrEnum):
    DB_STATE = "DB_STATE"
    API_RESULT = "API_RESULT"
    AUDIT = "AUDIT"
    TIMELINE = "TIMELINE"
    TRACE = "TRACE"
    LEDGER = "LEDGER"
    SCENARIO_RUN = "SCENARIO_RUN"
    ASSERTION_RESULT = "ASSERTION_RESULT"
    INVARIANT_RESULT = "INVARIANT_RESULT"
    FAULT_INJECTION_RESULT = "FAULT_INJECTION_RESULT"
    RUNTIME_PROVENANCE = "RUNTIME_PROVENANCE"
    TEST_REPORT = "TEST_REPORT"
    HUMAN_PROMOTION_RECORD = "HUMAN_PROMOTION_RECORD"


@dataclass(frozen=True, slots=True)
class EvidenceReferenceInput:
    tenant_id: str
    evidence_type: EvidenceType
    internal_entity_type: str | None = None
    internal_entity_id: str | None = None
    finding_id: str | None = None
    finding_occurrence_id: str | None = None
    external_ref: str | None = None
    artifact_path: str | None = None
    trace_id: str | None = None
    source_sha: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("EVIDENCE_TENANT_REQUIRED")
        has_internal = bool(self.internal_entity_type and self.internal_entity_id)
        if bool(self.internal_entity_type) != bool(self.internal_entity_id):
            raise ValueError("EVIDENCE_INTERNAL_REFERENCE_INCOMPLETE")
        if (
            not has_internal
            and not self.finding_id
            and not self.finding_occurrence_id
            and not self.external_ref
            and not self.artifact_path
            and not self.trace_id
        ):
            raise ValueError("EVIDENCE_REFERENCE_REQUIRED")
        if self.artifact_path:
            path = PurePosixPath(self.artifact_path)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("EVIDENCE_ARTIFACT_PATH_UNSAFE")
        for label, value, maximum in (
            ("internal_entity_id", self.internal_entity_id, 128),
            ("external_ref", self.external_ref, 320),
            ("trace_id", self.trace_id, 64),
            ("source_sha", self.source_sha, 128),
        ):
            if value is not None:
                try:
                    sanitized_summary(value, maximum_length=maximum)
                except ValueError as exc:
                    raise ValueError(f"EVIDENCE_{label.upper()}_UNSAFE") from exc
        sanitize_metadata(self.metadata)


def create_evidence_reference(
    session: Session,
    request: EvidenceReferenceInput,
    *,
    now: datetime | None = None,
) -> Any:
    """Create reference metadata only; evidence payload remains at its authority."""

    from attention_router.infrastructure.models import EvidenceReferenceRow

    timestamp = now or datetime.now(UTC)
    row = EvidenceReferenceRow(
        id=str(uuid4()),
        tenant_id=request.tenant_id,
        evidence_type=request.evidence_type.value,
        finding_id=request.finding_id,
        finding_occurrence_id=request.finding_occurrence_id,
        internal_entity_type=request.internal_entity_type,
        internal_entity_id=request.internal_entity_id,
        external_reference=request.external_ref,
        artifact_reference=request.artifact_path,
        trace_id=request.trace_id,
        source_sha=request.source_sha,
        sanitized_metadata=sanitize_metadata(request.metadata).value,
        created_at=timestamp,
    )
    session.add(row)
    session.flush()
    return row


def evidence_reference_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "evidence_type": row.evidence_type,
        "internal_entity_type": row.internal_entity_type,
        "internal_entity_id": row.internal_entity_id,
        "finding_id": row.finding_id,
        "finding_occurrence_id": row.finding_occurrence_id,
        "external_ref": row.external_reference,
        "artifact_path": row.artifact_reference,
        "trace_id": row.trace_id,
        "source_sha": row.source_sha,
        "metadata": dict(row.sanitized_metadata or {}),
        "created_at": row.created_at,
    }
