"""Offline, fail-closed budgeting of declared model usage.

No provider is enabled. Quotes reserve conservative ceilings BEFORE an SDK
call; measured usage reconciles afterwards. This ledger is in-memory only:
it is not durable, does not cancel a blocking provider call, and cannot
guarantee billing upper bounds when the provider exceeds its reservation.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock


class ModelBudgetDenied(RuntimeError):
    """Stable non-sensitive reason, never raw model responses."""


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    cost_micro_usd: int

    def __post_init__(self) -> None:
        for value in (self.input_tokens, self.output_tokens, self.cost_micro_usd):
            if type(value) is not int or value < 0:
                raise ValueError("MODEL_USAGE_INVALID")


@dataclass(frozen=True)
class ModelLimits:
    input_tokens: int
    output_tokens: int
    cost_micro_usd: int
    max_calls: int

    def __post_init__(self) -> None:
        for value in (
            self.input_tokens, self.output_tokens, self.cost_micro_usd, self.max_calls,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("MODEL_LIMIT_INVALID")


class ModelUsageGate:
    """A single investigation's quota, never an authority for SDK execution."""

    def __init__(self, limits: ModelLimits) -> None:
        self.limits = limits
        self._lock = Lock()
        self._inflight: dict[str, ModelUsage] = {}
        self._spent = ModelUsage(0, 0, 0)
        self._attempted_calls = 0
        self._seen_calls: set[str] = set()
        self._terminal_reason: str | None = None

    @property
    def terminal_reason(self) -> str | None:
        with self._lock:
            return self._terminal_reason

    @property
    def spent(self) -> ModelUsage:
        with self._lock:
            return self._spent

    def reserve(self, call_id: str, ceiling: ModelUsage) -> None:
        """Reserve a priced request BEFORE contacting any model.

        A caller must obtain a conservative, versioned quote externally.
        An unknown price is not encoded as zero; do not call this method
        without a real upper-bound quote.
        """
        if not isinstance(call_id, str) or not (1 <= len(call_id) <= 128):
            raise ModelBudgetDenied("MODEL_CALL_ID_INVALID")
        if not isinstance(ceiling, ModelUsage) or ceiling.cost_micro_usd == 0:
            # An unknown or zero-valued price estimate is not a safe default.
            # Explicit free-tier policy would need a separately reviewed flag.
            raise ModelBudgetDenied("MODEL_QUOTE_REQUIRED")
        with self._lock:
            if self._terminal_reason:
                raise ModelBudgetDenied(self._terminal_reason)
            if call_id in self._seen_calls:
                raise ModelBudgetDenied("MODEL_CALL_REPLAY")
            if self._attempted_calls >= self.limits.max_calls:
                raise ModelBudgetDenied("MODEL_CALL_LIMIT")
            committed = [
                self._spent.input_tokens, self._spent.output_tokens,
                self._spent.cost_micro_usd,
            ]
            for active in self._inflight.values():
                committed[0] += active.input_tokens
                committed[1] += active.output_tokens
                committed[2] += active.cost_micro_usd
            requested = [
                ceiling.input_tokens, ceiling.output_tokens, ceiling.cost_micro_usd,
            ]
            bounds = [
                self.limits.input_tokens, self.limits.output_tokens,
                self.limits.cost_micro_usd,
            ]
            if any(x + y > limit for x, y, limit in zip(committed, requested, bounds)):
                raise ModelBudgetDenied("MODEL_BUDGET_EXCEEDED")
            self._seen_calls.add(call_id)
            self._attempted_calls += 1
            self._inflight[call_id] = ceiling

    def settle(
        self, call_id: str, measured: ModelUsage | None, *, complete: bool = True,
    ) -> None:
        """Keep known tokens even if a later response fails; never mark partial as free."""
        with self._lock:
            ceiling = self._inflight.pop(call_id, None)
            if ceiling is None:
                raise ModelBudgetDenied("MODEL_CALL_NOT_RESERVED")
            if measured is None:
                self._terminal_reason = "MODEL_USAGE_UNKNOWN"
                return
            if not isinstance(measured, ModelUsage):
                self._terminal_reason = "MODEL_USAGE_INVALID"
                raise ModelBudgetDenied("MODEL_USAGE_INVALID")
            self._spent = ModelUsage(
                self._spent.input_tokens + measured.input_tokens,
                self._spent.output_tokens + measured.output_tokens,
                self._spent.cost_micro_usd + measured.cost_micro_usd,
            )
            if (
                measured.input_tokens > ceiling.input_tokens
                or measured.output_tokens > ceiling.output_tokens
                or measured.cost_micro_usd > ceiling.cost_micro_usd
            ):
                self._terminal_reason = "MODEL_RESERVATION_OVERRUN"
                raise ModelBudgetDenied(self._terminal_reason)
            if not complete:
                self._terminal_reason = "MODEL_USAGE_PARTIAL"

    def mark_failed_without_usage(self, call_id: str) -> None:
        """Provider error without trustworthy usage must not release the budget."""
        self.settle(call_id, None)
