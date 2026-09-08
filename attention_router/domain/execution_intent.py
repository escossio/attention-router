"""Canonical, inert pre-authorization execution intent."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, ClassVar
from uuid import uuid4


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ExecutionIntent:
    """Frozen semantic scope; creation has no operational side effects."""

    id: str
    idempotency_key: str
    scenario_version: str
    scenario_identity: dict[str, Any]
    safety_set: dict[str, Any]
    execution_class: str
    target: dict[str, Any]
    transport: dict[str, Any]
    budget_limits: dict[str, Any]
    operation_class: str
    immutable_inputs: dict[str, Any]
    policy_references: list[dict[str, Any]]
    requested_capability: str
    expiry_policy: dict[str, Any]
    provenance: dict[str, Any]
    state: str = "PREPARED"
    created_at: datetime = field(default_factory=_now)
    frozen_at: datetime | None = None
    retired_at: datetime | None = None
    fingerprint: str | None = None

    VALID_STATES: ClassVar[set[str]] = {"PREPARED", "FROZEN", "RETIRED", "MATERIALIZED"}

    def canonical_scope(self) -> dict[str, Any]:
        return {
            "scenario_version": self.scenario_version,
            "scenario_identity": self.scenario_identity,
            "safety_set": self.safety_set,
            "execution_class": self.execution_class,
            "target": self.target,
            "transport": self.transport,
            "budget_limits": self.budget_limits,
            "operation_class": self.operation_class,
            "immutable_inputs": self.immutable_inputs,
            "policy_references": self.policy_references,
            "requested_capability": self.requested_capability,
            "expiry_policy": self.expiry_policy,
        }

    def freeze(self) -> str:
        if self.state != "PREPARED":
            raise ValueError("only PREPARED intents can be frozen")
        self.fingerprint = hashlib.sha256(
            json.dumps(self.canonical_scope(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        self.state = "FROZEN"
        self.frozen_at = _now()
        return self.fingerprint

    def retire(self) -> None:
        if self.state not in {"PREPARED", "FROZEN"}:
            raise ValueError("only non-terminal intents can be retired")
        self.state = "RETIRED"
        self.retired_at = _now()

    def update(self, **_changes: Any) -> None:
        if self.state == "FROZEN":
            raise ValueError("frozen execution intent is immutable; create a new intent")
        if self.state != "PREPARED":
            raise ValueError("retired/materialized execution intent is immutable")
        raise ValueError("use a new intent for material changes")


def new_execution_intent(**scope: Any) -> ExecutionIntent:
    return ExecutionIntent(id=str(uuid4()), idempotency_key=scope.pop("idempotency_key"), **scope)
