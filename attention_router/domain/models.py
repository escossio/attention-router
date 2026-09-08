from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from attention_router.domain.enums import ActionState, InteractionState


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


@dataclass(frozen=True)
class ContactIdentity:
    synthetic_id: str
    display_name: str
    relationship_category: str


@dataclass
class Policy:
    identifier: str
    name: str
    match_criteria: dict[str, Any]
    priority: int
    specificity: int
    tone: str
    initial_wait_seconds: int
    allowed_disclosures: list[str]
    allowed_actions: list[str]
    escalation_steps: list[str]
    ack_timeout_seconds: int
    repetition_limit: int
    cancellation_conditions: list[str]
    completion_conditions: list[str]


@dataclass
class PolicyResolution:
    winner: Policy
    matched: list[dict[str, Any]]
    reason: str


@dataclass
class Interaction:
    id: str
    event_type: str
    contact: ContactIdentity
    active_context: str | None
    inbound_text: str
    state: InteractionState = InteractionState.RECEIVED
    policy_id: str | None = None
    lia_speech: str | None = None
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)


@dataclass
class Decision:
    id: str
    interaction_id: str
    policy_id: str
    matched_rules: list[dict[str, Any]]
    reason: str
    created_at: datetime = field(default_factory=now_utc)


@dataclass
class ActionAttempt:
    id: str
    interaction_id: str
    action_key: str
    step_index: int
    state: ActionState = ActionState.REQUESTED
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)


@dataclass
class Acknowledgement:
    id: str
    interaction_id: str
    action_attempt_id: str
    source: str
    created_at: datetime = field(default_factory=now_utc)


@dataclass
class AuditEvent:
    id: str
    interaction_id: str | None
    event_type: str
    payload: dict[str, Any]
    created_at: datetime = field(default_factory=now_utc)
