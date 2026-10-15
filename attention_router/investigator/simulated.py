"""Synthetic adapters for contract development; never connect to live systems."""

from dataclasses import dataclass, field
from typing import Any

from .model import Conclusion, Evidence, Findings, Hypothesis, Query, SourceEvent
from .ports import QueryAccess

SOURCES = ("zabbix", "tempo", "postgresql", "linux", "distributed_ci", "afo_catalog")


@dataclass
class SimulatedConnector:
    source: str
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    fail: bool = False

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError("UNKNOWN_SOURCE")

    def read(self, query: Query) -> Evidence:
        if query.source != self.source:
            raise ValueError("CONNECTOR_SOURCE_MISMATCH")
        if self.fail:
            raise ConnectionError("synthetic failure")
        if query.operation not in self.responses:
            raise KeyError("synthetic operation unavailable")
        return Evidence(query, self.responses[query.operation].copy(),
                        f"synthetic:{self.source}:{query.operation}")


@dataclass
class MemoryStore:
    records: list = field(default_factory=list)

    def save(self, investigation) -> None:
        self.records.append(investigation)


@dataclass
class MemoryAudit:
    events: list[tuple[str, str, str]] = field(default_factory=list)

    def record(self, investigation_id: str, correlation_id: str, event: str) -> None:
        self.events.append((investigation_id, correlation_id, event))


@dataclass
class ScriptedRuntime:
    """Simulates an existing agent runtime; no agent loop or API call."""

    queries: tuple[Query, ...]

    def investigate(self, event: SourceEvent, queries: QueryAccess) -> Findings:
        evidence = tuple(queries.read(query) for query in self.queries)
        indexes = tuple(range(len(evidence)))
        return Findings(evidence, (Hypothesis("synthetic hypothesis", indexes, "unverified"),),
                        Conclusion("synthetic conclusion", indexes, ("simulated sources",)))
