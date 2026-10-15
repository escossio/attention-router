"""Investigation data kept separate from existing conversation contracts."""

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class SourceEvent:
    source: str
    original: bytes
    content_type: str
    tenant_id: str
    source_event_id: str | None = None
    correlation_id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        if not self.tenant_id.strip():
            raise ValueError("TENANT_REQUIRED")

    @property
    def digest(self) -> str:
        return sha256(self.original).hexdigest()


@dataclass(frozen=True)
class Query:
    source: str
    operation: str
    arguments: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Evidence:
    query: Query
    observation: dict[str, Any]
    source_reference: str


@dataclass(frozen=True)
class Hypothesis:
    statement: str
    evidence_indexes: tuple[int, ...]
    uncertainty: str


@dataclass(frozen=True)
class Conclusion:
    summary: str
    evidence_indexes: tuple[int, ...]
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class Findings:
    evidence: tuple[Evidence, ...]
    hypotheses: tuple[Hypothesis, ...]
    conclusion: Conclusion | None


@dataclass(frozen=True)
class Investigation:
    investigation_id: str
    event: SourceEvent
    findings: Findings | None
    status: str
    query_count: int
    error_code: str | None = None
    # Provenance-backed tool observations stay available if the model fails or hits a limit.
    # A production store must restrict access; tool payloads must not enter generic logs.
    partial_evidence: tuple[Evidence, ...] = ()
