"""Bounded read-only investigation around an externally supplied agent runtime.

Offline V0 extension: tool calls are audited without raw arguments or tool responses.
Runtime integration, durable seed admission and hard cancellation remain separate gates.
"""

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from time import monotonic
from uuid import UUID, uuid4

from .model import Findings, Investigation, Query, SourceEvent
from .ports import ActivitySink, AgentRuntime, AuditSink, InvestigationStore, ReadOnlyConnector


@dataclass(frozen=True)
class Budget:
    max_queries: int
    max_seconds: float
    # Serialized observation + reference sizes; measured after each read and
    # before retaining a model-visible copy. Does not cap connector IO itself.
    max_evidence_bytes: int = 16384
    max_total_evidence_bytes: int = 65536

    def __post_init__(self) -> None:
        if self.max_queries < 1 or self.max_seconds <= 0:
            raise ValueError("INVALID_BUDGET")
        if (
            type(self.max_evidence_bytes) is not int
            or type(self.max_total_evidence_bytes) is not int
            or self.max_evidence_bytes < 1
            or self.max_total_evidence_bytes < 1
        ):
            raise ValueError("INVALID_EVIDENCE_BUDGET")


class BudgetExceeded(RuntimeError):
    pass


class ConnectorUnavailable(RuntimeError):
    pass


class AuthorizationDenied(RuntimeError):
    pass


class _BoundedQueries:
    def __init__(
        self, connectors: dict[str, ReadOnlyConnector], budget: Budget,
        allowed_operations: dict[str, frozenset[str]], audit: AuditSink,
        investigation_id: str, correlation_id: str,
        activity_sink: ActivitySink | None = None,
    ) -> None:
        self.connectors = connectors
        self.budget = budget
        self.allowed_operations = allowed_operations
        self.audit = audit
        self.investigation_id = investigation_id
        self.correlation_id = correlation_id
        self.activity_sink = activity_sink
        self.started = monotonic()
        self.count = 0
        self.observed = []
        self.observed_bytes = 0

    def catalog(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(
            (source, operation)
            for source, operations in self.allowed_operations.items()
            if source in self.connectors
            for operation in operations
        ))

    def _record(self, event: str) -> None:
        self.audit.record(self.investigation_id, self.correlation_id, event)

    def _signal(self, event: str, query: Query | None = None, ordinal: int | None = None) -> None:
        # Telemetry is best-effort and never authorizes or blocks an investigation.
        if self.activity_sink is None:
            return
        try:
            self.activity_sink.emit(
                self.investigation_id, event,
                source=query.source if query is not None else None,
                operation=query.operation if query is not None else None,
                ordinal=ordinal,
            )
        except Exception:
            pass

    def check_time(self) -> None:
        if monotonic() - self.started >= self.budget.max_seconds:
            raise BudgetExceeded("TIME_BUDGET_EXCEEDED")

    def read(self, query: Query):
        # Fingerprint complete request fields without exposing sensitive arguments.
        fingerprint = sha256(json.dumps(
            {"source": query.source, "operation": query.operation, "arguments": query.arguments},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")).hexdigest()[:16]
        serial = self.count + 1
        self._record(f"query.{serial}.requested:{fingerprint}")
        self._signal("query_requested", query, serial)
        try:
            self.check_time()
        except BudgetExceeded:
            self._record(f"query.time_budget_rejected:{fingerprint}")
            self._signal("query_limited", query, serial)
            raise
        if self.count >= self.budget.max_queries:
            self._record(f"query.query_budget_rejected:{fingerprint}")
            self._signal("query_limited", query, serial)
            raise BudgetExceeded("QUERY_BUDGET_EXCEEDED")

        self.count += 1

        # The allowlist is supplied by an external, trusted authority, not the model.
        if query.operation not in self.allowed_operations.get(query.source, frozenset()):
            self._record(f"query.{serial}.denied:{fingerprint}")
            self._signal("query_denied", query, serial)
            raise AuthorizationDenied("OPERATION_NOT_ALLOWED")
        connector = self.connectors.get(query.source)
        if connector is None:
            self._record(f"query.{serial}.unavailable:{fingerprint}")
            self._signal("query_failed", query, serial)
            raise ConnectorUnavailable("CONNECTOR_UNAVAILABLE")

        try:
            result = connector.read(deepcopy(query))
            if result.query != query:
                raise ValueError("EVIDENCE_QUERY_MISMATCH")
            # Account for the entire evidence payload before making a retained
            # copy. No raw payload or serialized data enters the audit events.
            try:
                encoded = json.dumps(
                    {"observation": result.observation,
                     "source_reference": result.source_reference},
                    ensure_ascii=True, allow_nan=False, separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            except (TypeError, ValueError, OverflowError):
                raise ValueError("EVIDENCE_ENCODING_INVALID") from None

            size = len(encoded)
            if size > self.budget.max_evidence_bytes:
                raise BudgetExceeded("EVIDENCE_BYTES_PER_QUERY_EXCEEDED")
            if self.observed_bytes + size > self.budget.max_total_evidence_bytes:
                raise BudgetExceeded("EVIDENCE_BYTES_TOTAL_EXCEEDED")
            # A private, independent copy is retained before the model sees it.
            snapshot = deepcopy(result)
            self.observed.append(snapshot)
            self.observed_bytes += size
        except BudgetExceeded:
            self._record(f"query.{serial}.limited:{fingerprint}")
            self._signal("query_limited", query, serial)
            raise
        except Exception:
            self._record(f"query.{serial}.failed:{fingerprint}")
            self._signal("query_failed", query, serial)
            raise

        self._record(f"query.{serial}.observed:{fingerprint}")
        self._signal("query_observed", query, serial)
        # Slow/blocking calls cannot yet be preempted, but their returned evidence
        # is not discarded if the deadline was exceeded while reading.
        self.check_time()
        return deepcopy(snapshot)


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
        *,
        authorized_tenants: frozenset[str] = frozenset(),
        allowed_operations: dict[str, frozenset[str]] | None = None,
        activity_sink: ActivitySink | None = None,
    ) -> None:
        if any(source != connector.source for source, connector in connectors.items()):
            raise ValueError("CONNECTOR_SOURCE_MISMATCH")
        self.runtime = runtime
        self.connectors = connectors
        self.store = store
        self.audit = audit
        self.budget = budget
        self.activity_sink = activity_sink
        # No implicit ambient tenant authority or tool access.
        self.authorized_tenants = frozenset(authorized_tenants)
        self.allowed_operations = {
            source: frozenset(ops) for source, ops in (allowed_operations or {}).items()
        }

    def investigate(
        self, event: SourceEvent, *, investigation_id: str | None = None,
    ) -> Investigation:
        if investigation_id is None:
            investigation_id = str(uuid4())
        else:
            if type(investigation_id) is not str:
                raise ValueError("INVALID_INVESTIGATION_ID")
            try:
                parsed_id = UUID(investigation_id)
            except (ValueError, AttributeError) as exc:
                raise ValueError("INVALID_INVESTIGATION_ID") from exc
            if parsed_id.version != 4 or str(parsed_id) != investigation_id:
                raise ValueError("INVALID_INVESTIGATION_ID")
        queries = _BoundedQueries(
            self.connectors, self.budget, self.allowed_operations,
            self.audit, investigation_id, event.correlation_id, self.activity_sink,
        )
        self.audit.record(investigation_id, event.correlation_id, "started")
        queries._signal("started")
        findings = None
        status = "completed"
        error_code = None
        try:
            # The future admission adapter must bind tenant_id from a trusted
            # credential/source; this guard merely rejects mismatched scope.
            if event.tenant_id not in self.authorized_tenants:
                raise AuthorizationDenied("TENANT_NOT_AUTHORIZED")
            findings = self.runtime.investigate(event, queries)
            queries.check_time()
            _validate(findings, queries.observed)
        except AuthorizationDenied as exc:
            status, error_code = "blocked", str(exc)
        except BudgetExceeded as exc:
            status, error_code = "limited", str(exc)
        except (ConnectorUnavailable, ValueError) as exc:
            status, error_code = "failed", str(exc)
        except Exception:
            status, error_code = "failed", "RUNTIME_OR_CONNECTOR_FAILED"
        result = Investigation(
            investigation_id, event, findings, status, queries.count, error_code,
            tuple(deepcopy(queries.observed)),
        )
        self.store.save(result)
        self.audit.record(investigation_id, event.correlation_id, status)
        queries._signal(status)
        return result
