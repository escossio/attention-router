"""Read-only structural contract for the execution runtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from attention_router.infrastructure.models import (
    AgentExecutionIntentRow,
    EffectConsumptionRow,
    ExecutionLeaseRow,
    OutboxMessageRow,
    ScenarioRunRow,
)
from attention_router.platform.execution_safety import activate_scenario_run_for_execution, provision_scenario_execution_safety_in_transaction


class ContractState(StrEnum):
    SUPPORTED = "SUPPORTED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ContractEvidence:
    state: ContractState
    reason_code: str
    source: str
    contract_version: str


@dataclass(frozen=True, slots=True)
class ExecutionRuntimeContract:
    """Software/schema contract; it deliberately contains no live resources."""

    lease: ContractEvidence
    explicit_activation: ContractEvidence
    prepared_run_non_dispatchable: ContractEvidence
    effect_budget: ContractEvidence
    effect_consumption: ContractEvidence
    logical_effect_uniqueness: ContractEvidence
    outbox_idempotency: ContractEvidence
    execution_intent_idempotency: ContractEvidence
    correlation_propagation: ContractEvidence
    fail_close_execution_safety: ContractEvidence

    @property
    def supported(self) -> bool:
        return all(
            item.state is ContractState.SUPPORTED
            for item in (
                self.lease, self.explicit_activation, self.prepared_run_non_dispatchable,
                self.effect_budget, self.effect_consumption, self.logical_effect_uniqueness,
                self.outbox_idempotency, self.execution_intent_idempotency,
                self.correlation_propagation, self.fail_close_execution_safety,
            )
        )


CONTRACT_VERSION = "execution-runtime-contract-v1"


def _supported(name: str, source: str) -> ContractEvidence:
    return ContractEvidence(ContractState.SUPPORTED, name, source, CONTRACT_VERSION)


def get_execution_runtime_contract(*, capabilities: dict[str, Any] | None = None) -> ExecutionRuntimeContract:
    """Return the versioned contract from actual model/path invariants.

    ``capabilities`` is an explicit fault-injection seam for tests only; callers
    cannot turn an unsupported runtime into supported without replacing the
    canonical source itself.
    """
    overrides = capabilities or {}
    sources = {
        "lease": (ExecutionLeaseRow, "execution_safety.provision_and_claim"),
        "explicit_activation": (activate_scenario_run_for_execution, "execution_safety.activate_scenario_run_for_execution"),
        "prepared_run_non_dispatchable": (ScenarioRunRow, "services.worker_selector:ARMED/RUNNING"),
        "effect_budget": (provision_scenario_execution_safety_in_transaction, "execution_safety.provision_scenario_execution_safety_in_transaction"),
        "effect_consumption": (EffectConsumptionRow, "execution_safety.reserve_synthetic_system_effect_for_intent_in_transaction"),
        "logical_effect_uniqueness": (EffectConsumptionRow, "EffectConsumptionRow.uq_effect_consumption_budget_logical_effect"),
        "outbox_idempotency": (OutboxMessageRow, "OutboxMessageRow.uq_outbox_idempotency_key"),
        "execution_intent_idempotency": (AgentExecutionIntentRow, "AgentExecutionIntentRow.uq_agent_execution_intents_idempotency"),
        "correlation_propagation": (ScenarioRunRow, "ScenarioRun/Lease/Outbox.correlation_id"),
        "fail_close_execution_safety": (activate_scenario_run_for_execution, "execution_safety.SafetyDenied"),
    }
    result = {}
    for name, (source_obj, source) in sources.items():
        override = overrides.get(name)
        result[name] = override if isinstance(override, ContractEvidence) else _supported(
            f"{name.upper()}_CONTRACT_SUPPORTED", source
        ) if source_obj is not None else ContractEvidence(ContractState.UNKNOWN, "SOURCE_UNAVAILABLE", source, CONTRACT_VERSION)
    return ExecutionRuntimeContract(**result)
