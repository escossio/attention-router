from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from attention_router.core.events import EventEnvelope


class RecentContextRetriever(Protocol):
    def retrieve(self, tenant_id: str, actor_id: str | None, limit: int) -> list[dict[str, Any]]: ...


class HistoricalContextRetriever(Protocol):
    def retrieve(self, tenant_id: str, actor_id: str | None, limit: int) -> list[dict[str, Any]]: ...


class MemoryRetriever(Protocol):
    def retrieve(self, tenant_id: str, actor_id: str | None, limit: int) -> list[dict[str, Any]]: ...


class StateRetriever(Protocol):
    def retrieve(self, tenant_id: str, actor_id: str | None, limit: int) -> list[dict[str, Any]]: ...


class FactRetriever(Protocol):
    def retrieve(self, tenant_id: str, actor_id: str | None, limit: int) -> list[dict[str, Any]]: ...


class CapabilityContextProvider(Protocol):
    def retrieve(self, tenant_id: str, actor_id: str | None, limit: int) -> list[dict[str, Any]]: ...


class RelevantDecisionRetriever(Protocol):
    def retrieve(self, tenant_id: str, actor_id: str | None, limit: int) -> list[dict[str, Any]]: ...


class EffectiveAuthorityProvider(Protocol):
    def retrieve(self, tenant_id: str, actor_id: str | None, limit: int) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ContextSnapshot:
    current_event: EventEnvelope
    recent_conversation: list[dict[str, Any]] = field(default_factory=list)
    relevant_historical_context: list[dict[str, Any]] = field(default_factory=list)
    persistent_memory: list[dict[str, Any]] = field(default_factory=list)
    current_operational_state: list[dict[str, Any]] = field(default_factory=list)
    relevant_facts: list[dict[str, Any]] = field(default_factory=list)
    relevant_decisions: list[dict[str, Any]] = field(default_factory=list)
    available_capabilities: list[dict[str, Any]] = field(default_factory=list)
    effective_authority: list[dict[str, Any]] = field(default_factory=list)

    def sanitized_prompt_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        event = payload["current_event"]
        event.pop("actor_id", None)
        event.pop("payload_ref", None)
        return payload


class ContextCoreBuilder:
    """Composes bounded context without knowing storage table details."""

    def __init__(
        self,
        *,
        recent: RecentContextRetriever | None = None,
        historical: HistoricalContextRetriever | None = None,
        memory: MemoryRetriever | None = None,
        state: StateRetriever | None = None,
        facts: FactRetriever | None = None,
        capabilities: CapabilityContextProvider | None = None,
        decisions: RelevantDecisionRetriever | None = None,
        authority: EffectiveAuthorityProvider | None = None,
    ) -> None:
        self.recent = recent
        self.historical = historical
        self.memory = memory
        self.state = state
        self.facts = facts
        self.capabilities = capabilities
        self.decisions = decisions
        self.authority = authority

    @staticmethod
    def _read(source: Any, tenant_id: str, actor_id: str | None, limit: int) -> list[dict[str, Any]]:
        return source.retrieve(tenant_id, actor_id, limit) if source is not None else []

    def build(
        self,
        event: EventEnvelope,
        *,
        limit: int = 20,
        state_actor_id: str | None = None,
    ) -> ContextSnapshot:
        tenant_id, actor_id = event.tenant_id, event.actor_id
        return ContextSnapshot(
            current_event=event,
            recent_conversation=self._read(self.recent, tenant_id, actor_id, limit),
            relevant_historical_context=self._read(self.historical, tenant_id, actor_id, limit),
            persistent_memory=self._read(self.memory, tenant_id, actor_id, limit),
            current_operational_state=self._read(self.state, tenant_id, state_actor_id or actor_id, limit),
            relevant_facts=self._read(self.facts, tenant_id, actor_id, limit),
            relevant_decisions=self._read(self.decisions, tenant_id, actor_id, limit),
            available_capabilities=self._read(self.capabilities, tenant_id, actor_id, limit),
            effective_authority=self._read(self.authority, tenant_id, actor_id, limit),
        )
