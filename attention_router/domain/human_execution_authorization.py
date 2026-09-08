"""Human approval of one frozen ExecutionIntent."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from attention_router.domain.execution_intent import ExecutionIntent


@dataclass
class HumanExecutionAuthorization:
    id: str
    execution_intent_id: str
    execution_intent_fingerprint: str
    expected_approver: str
    approval_channel: str
    request_metadata: dict[str, Any]
    issued_at: datetime
    expires_at: datetime
    state: str = "PENDING_HUMAN_APPROVAL"
    decision_at: datetime | None = None
    decision_sender: str | None = None

    @classmethod
    def prepare(cls, intent: ExecutionIntent, expected_approver: str, approval_channel: str,
                ttl_seconds: int, request_metadata: dict[str, Any] | None = None) -> "HumanExecutionAuthorization":
        if intent.state != "FROZEN" or not intent.fingerprint:
            raise ValueError("authorization requires a frozen execution intent")
        issued = datetime.now(timezone.utc)
        return cls(str(uuid4()), intent.id, intent.fingerprint, expected_approver,
                   approval_channel, request_metadata or {}, issued,
                   issued + timedelta(seconds=ttl_seconds))

    def _validate(self, intent: ExecutionIntent, sender: str, now: datetime) -> None:
        if self.state != "PENDING_HUMAN_APPROVAL":
            raise ValueError("authorization is already decided")
        if sender != self.expected_approver:
            raise ValueError("unexpected approver")
        if now >= self.expires_at:
            self.state = "EXPIRED"
            raise ValueError("authorization expired")
        if intent.state != "FROZEN" or intent.id != self.execution_intent_id:
            raise ValueError("execution intent is no longer authorizable")
        if intent.fingerprint != self.execution_intent_fingerprint:
            raise ValueError("execution intent fingerprint mismatch")

    def approve(self, intent: ExecutionIntent, sender: str, now: datetime | None = None) -> None:
        decision_at = now or datetime.now(timezone.utc)
        self._validate(intent, sender, decision_at)
        self.state, self.decision_at, self.decision_sender = "APPROVED", decision_at, sender

    def deny(self, intent: ExecutionIntent, sender: str, now: datetime | None = None) -> None:
        decision_at = now or datetime.now(timezone.utc)
        self._validate(intent, sender, decision_at)
        self.state, self.decision_at, self.decision_sender = "DENIED", decision_at, sender
