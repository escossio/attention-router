"""Ports only: no production credentials, writes, or runtime selection."""

from typing import Protocol

from .model import Evidence, Findings, Investigation, Query, SourceEvent


class ReadOnlyConnector(Protocol):
    source: str

    def read(self, query: Query) -> Evidence: ...


class QueryAccess(Protocol):
    def read(self, query: Query) -> Evidence: ...


class AgentRuntime(Protocol):
    def investigate(self, event: SourceEvent, queries: QueryAccess) -> Findings: ...


class InvestigationStore(Protocol):
    """Future isolated PostgreSQL store; caller owns transaction and tenant scope."""

    def save(self, investigation: Investigation) -> None: ...


class AuditSink(Protocol):
    """Audit metadata only; never send original event or tool response to logs."""

    def record(self, investigation_id: str, correlation_id: str, event: str) -> None: ...
