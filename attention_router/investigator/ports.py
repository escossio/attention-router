"""Ports only: no production credentials, writes, or runtime selection."""

from typing import Protocol

from .model import Evidence, Findings, Investigation, Query, SourceEvent


class ReadOnlyConnector(Protocol):
    source: str

    def read(self, query: Query) -> Evidence: ...


class QueryAccess(Protocol):
    def read(self, query: Query) -> Evidence: ...

    def catalog(self) -> tuple[tuple[str, str], ...]:
        """Authorized source/operation pairs only; never observations."""
        ...


class AgentRuntime(Protocol):
    def investigate(self, event: SourceEvent, queries: QueryAccess) -> Findings: ...


class InvestigationStore(Protocol):
    """Future isolated PostgreSQL store; caller owns transaction and tenant scope."""

    def save(self, investigation: Investigation) -> None: ...


class ActivitySink(Protocol):
    """Optional metadata-only stream for a read-only operations dashboard."""

    def emit(
        self, run_id: str, event: str, *,
        source: str | None = None,
        operation: str | None = None,
        ordinal: int | None = None,
    ) -> None: ...


class AuditSink(Protocol):
    """Audit metadata only; never send original event or tool response to logs."""

    def record(self, investigation_id: str, correlation_id: str, event: str) -> None: ...
