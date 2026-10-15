"""Bounded read-only invocation around an externally supplied agent runtime."""

from dataclasses import dataclass
from time import monotonic
from uuid import uuid4

from .model import Findings, Investigation, Query, SourceEvent
from .ports import AgentRuntime, AuditSink, InvestigationStore, ReadOnlyConnector


@dataclass(frozen=True)
class Budget:
    max_queries: int
    max_seconds: float

    def __post_init__(self) -> None:
        if self.max_queries < 1 or self.max_seconds <= 0:
            raise ValueError("INVALID_BUDGET")


class BudgetExceeded(RuntimeError):
    pass


class ConnectorUnavailable(RuntimeError):
    pass


class _BoundedQueries:
    def __init__(self, connectors: dict[str, ReadOnlyConnector], budget: Budget) -> None:
        self.connectors = connectors
        self.budget = budget
        self.started = monotonic()
        self.count = 0
        self.observed = []

    def check_time(self) -> None:
        if monotonic() - self.started >= self.budget.max_seconds:
            raise BudgetExceeded("TIME_BUDGET_EXCEEDED")

    def read(self, query: Query):
        self.check_time()
        if self.count >= self.budget.max_queries:
            raise BudgetExceeded("QUERY_BUDGET_EXCEEDED")
        connector = self.connectors.get(query.source)
        if connector is None:
            raise ConnectorUnavailable("CONNECTOR_UNAVAILABLE")
        self.count += 1
        result = connector.read(query)
        self.check_time()
        if result.query != query:
            raise ValueError("EVIDENCE_QUERY_MISMATCH")
        self.observed.append(result)
        return result


def _validate(findings: Findings, observed: list) -> None:
    if any(evidence not in observed for evidence in findings.evidence):
        raise ValueError("UNOBSERVED_EVIDENCE")
    valid = set(range(len(findings.evidence)))
    for hypothesis in findings.hypotheses:
        if not set(hypothesis.evidence_indexes) <= valid:
            raise ValueError("INVALID_HYPOTHESIS_REFERENCE")
    if findings.conclusion and not set(findings.conclusion.evidence_indexes) <= valid:
        raise ValueError("INVALID_CONCLUSION_REFERENCE")


class Investigator:
    def __init__(
        self, runtime: AgentRuntime, connectors: dict[str, ReadOnlyConnector],
        store: InvestigationStore, audit: AuditSink, budget: Budget,
    ) -> None:
        if any(source != connector.source for source, connector in connectors.items()):
            raise ValueError("CONNECTOR_SOURCE_MISMATCH")
        self.runtime = runtime
        self.connectors = connectors
        self.store = store
        self.audit = audit
        self.budget = budget

    def investigate(self, event: SourceEvent) -> Investigation:
        investigation_id = str(uuid4())
        queries = _BoundedQueries(self.connectors, self.budget)
        self.audit.record(investigation_id, event.correlation_id, "started")
        findings = None
        status = "completed"
        error_code = None
        try:
            findings = self.runtime.investigate(event, queries)
            queries.check_time()
            _validate(findings, queries.observed)
        except BudgetExceeded as exc:
            status, error_code = "limited", str(exc)
        except (ConnectorUnavailable, ValueError) as exc:
            status, error_code = "failed", str(exc)
        except Exception:
            status, error_code = "failed", "RUNTIME_OR_CONNECTOR_FAILED"
        result = Investigation(investigation_id, event, findings, status, queries.count, error_code)
        self.store.save(result)
        self.audit.record(investigation_id, event.correlation_id, status)
        return result
