"""Durable first presentation bookkeeping; never authorizes or sends an effect.

The caller owns short transactions and MUST commit before model/provider/HTTP
work. Reservations do not expire. A lost reservation needs explicit, evidenced
administrative reconciliation; normal conversation can continue without intro.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from attention_router.application.direct_conversation import direct_conversation_eligibility
from attention_router.application.first_contact_identity import canonical_peer_hash
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.first_contact_models import (
    FirstContactAttemptRow, FirstContactPresentationRow,
)
from attention_router.infrastructure.models import (
    ActorBindingRow, AgentDecisionRow, AgentExecutionIntentRow, AgentResponseReviewRow, InboundEventRow,
    InteractionRow, OutboxMessageRow,
)
from attention_router.infrastructure.repository import audit


PRESENTATION_VERSION = "first-contact-v1"
_HASH = re.compile(r"[a-f0-9]{64}\Z")


class FirstContactConflict(ValueError):
    pass


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise FirstContactConflict(reason)


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FirstContactScope:
    tenant_id: str = field(repr=False)
    source: str
    channel: str
    source_account: str = field(repr=False)
    canonical_peer_hash: str = field(repr=False)

    def __post_init__(self):
        _require(isinstance(self.tenant_id, str) and 0 < len(self.tenant_id) <= 64,
                 "FIRST_CONTACT_TENANT_REQUIRED")
        _require(self.source == "wwebjs" and self.channel == "whatsapp",
                 "FIRST_CONTACT_DIRECT_SOURCE_REQUIRED")
        _require(isinstance(self.source_account, str) and 0 < len(self.source_account) <= 180
                 and self.source_account == self.source_account.strip(),
                 "FIRST_CONTACT_ACCOUNT_REQUIRED")
        _require(isinstance(self.canonical_peer_hash, str) and bool(_HASH.fullmatch(self.canonical_peer_hash)),
                 "FIRST_CONTACT_CANONICAL_IDENTITY_REQUIRED")

    def values(self) -> dict:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class ReservationToken:
    scope_id: str
    attempt_id: str
    generation: int
    event_id: str
    presentation_version: str


@dataclass(frozen=True)
class ReservationResult:
    status: str
    token: ReservationToken | None = None


def _scope_conditions(scope: FirstContactScope):
    return [getattr(FirstContactPresentationRow, name) == value
            for name, value in scope.values().items()]


def _lock_reservation_scope(session: Session, scope: FirstContactScope) -> bool:
    """Fail fast even when the unique row has not been committed yet."""
    if session.bind.dialect.name != "postgresql":
        return True
    digest = text_hash(json.dumps(scope.values(), sort_keys=True, separators=(",", ":")))
    key = int(digest[:16], 16)
    if key >= 2**63:
        key -= 2**64
    return bool(session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}))


def _event(session: Session, event_id: str, scope: FirstContactScope) -> InboundEventRow:
    event = session.get(InboundEventRow, event_id, populate_existing=True)
    _require(event is not None and event.tenant_id == scope.tenant_id and event.source == scope.source,
             "FIRST_CONTACT_EVENT_SCOPE_MISMATCH")
    metadata = (event.payload or {}).get("metadata") or {}
    _require(metadata.get("source_account") == scope.source_account
             and (event.payload or {}).get("channel") == scope.channel
             and direct_conversation_eligibility(event).eligible
             and canonical_peer_hash(event) == scope.canonical_peer_hash,
             "FIRST_CONTACT_EVENT_NOT_ELIGIBLE")
    interaction = session.get(InteractionRow, event.interaction_id)
    _require(interaction is not None and interaction.tenant_id == scope.tenant_id
             and interaction.relationship_category == "unknown", "FIRST_CONTACT_AUDIENCE_DENIED")
    aliases = set(metadata.get("peer_identifiers") or [])
    aliases.add(str((event.payload or {}).get("actor_id", interaction.contact_id)))
    aliases.add(str((event.payload or {}).get("external_actor_id", interaction.contact_id)))
    binding = session.scalar(select(ActorBindingRow.id).where(
        ActorBindingRow.tenant_id == scope.tenant_id, ActorBindingRow.source == scope.source,
        ActorBindingRow.external_actor_id.in_(aliases), ActorBindingRow.is_active.is_(True),
    ).limit(1))
    _require(binding is None, "FIRST_CONTACT_BINDING_DENIED")
    return event


def _token(row: FirstContactPresentationRow, attempt: FirstContactAttemptRow) -> ReservationToken:
    return ReservationToken(row.id, attempt.id, attempt.generation, attempt.event_id,
                            attempt.presentation_version)


def _audit(session: Session, row: FirstContactPresentationRow,
           attempt: FirstContactAttemptRow, event_type: str) -> None:
    event = session.get(InboundEventRow, attempt.event_id)
    audit(session, event.interaction_id if event else None, "first_contact." + event_type,
          {"presentation_id": row.id, "attempt_id": attempt.id,
           "generation": attempt.generation, "state": attempt.state},
          origin="first_contact", tenant_id=row.tenant_id)


def reserve(session: Session, scope: FirstContactScope, event_id: str,
            presentation_version: str = PRESENTATION_VERSION) -> ReservationResult:
    """Only RESERVED with a new token permits the caller to generate an intro.

    REPLAY has no token: even the same event cannot launch two model runs.
    FAILED permits only a newer, previously unseen event. There is no TTL steal.
    """
    _require(presentation_version == PRESENTATION_VERSION, "FIRST_CONTACT_VERSION_UNSUPPORTED")
    session.flush()
    event = _event(session, event_id, scope)
    if not _lock_reservation_scope(session, scope):
        return ReservationResult("BUSY")
    row = session.scalar(select(FirstContactPresentationRow).where(
        *_scope_conditions(scope),
    ).with_for_update().execution_options(populate_existing=True))
    prior = session.scalar(select(FirstContactAttemptRow).where(
        FirstContactAttemptRow.event_id == event_id,
    ))
    if prior is not None:
        return ReservationResult("REPLAY")
    if row is not None:
        if row.state == "CONFIRMED":
            return ReservationResult("CONFIRMED")
        if row.state != "FAILED":
            return ReservationResult("BUSY")
        previous = session.get(FirstContactAttemptRow, row.current_attempt_id)
        previous_event = session.get(InboundEventRow, previous.event_id) if previous else None
        _require(previous is not None and previous.state == "FAILED" and previous_event is not None,
                 "FIRST_CONTACT_STATE_INCONSISTENT")
        newer = _utc(event.received_at) > _utc(previous_event.received_at)
        if not newer:
            return ReservationResult("REPLAY")
    stamp = now_utc()
    if row is None:
        row = FirstContactPresentationRow(
            id=new_id(), **scope.values(), presentation_version=presentation_version,
            state="RESERVED", current_attempt_id=None, generation=1, revision=0,
            created_at=stamp, updated_at=stamp, confirmed_at=None,
        )
        session.add(row)
        session.flush()
    else:
        row.generation += 1
        row.revision += 1
        row.state, row.updated_at = "RESERVED", stamp
        row.presentation_version = presentation_version
    attempt = FirstContactAttemptRow(
        id=new_id(), presentation_id=row.id, event_id=event_id, generation=row.generation,
        presentation_version=presentation_version, state="RESERVED", created_at=stamp, updated_at=stamp,
    )
    session.add(attempt)
    session.flush()
    row.current_attempt_id = attempt.id
    _audit(session, row, attempt, "reserved")
    session.flush()
    return ReservationResult("RESERVED", _token(row, attempt))


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _locked(session: Session, attempt_id: str):
    session.flush()
    owner = session.scalar(select(FirstContactAttemptRow.presentation_id).where(
        FirstContactAttemptRow.id == attempt_id,
    ))
    _require(owner is not None, "FIRST_CONTACT_ATTEMPT_UNKNOWN")
    row = session.scalar(select(FirstContactPresentationRow).where(
        FirstContactPresentationRow.id == owner,
    ).with_for_update().execution_options(populate_existing=True))
    attempt = session.get(FirstContactAttemptRow, attempt_id, populate_existing=True)
    _require(row is not None and attempt is not None and row.current_attempt_id == attempt.id
             and row.generation == attempt.generation, "FIRST_CONTACT_STALE_ATTEMPT")
    return row, attempt


def _with_token(session: Session, token: ReservationToken):
    _require(isinstance(token, ReservationToken), "FIRST_CONTACT_TOKEN_REQUIRED")
    row, attempt = _locked(session, token.attempt_id)
    _require(_token(row, attempt) == token, "FIRST_CONTACT_STALE_RESERVATION")
    return row, attempt


def _transition(session: Session, row, attempt, state: str, *, event_type: str | None = None):
    if row.state == "CONFIRMED":
        _require(state == "CONFIRMED", "FIRST_CONTACT_CONFIRMED_IMMUTABLE")
        return False
    revision = row.revision
    stamp = now_utc()
    values = {"state": state, "updated_at": stamp, "revision": revision + 1}
    if state == "CONFIRMED":
        values["confirmed_at"] = stamp
    result = session.execute(update(FirstContactPresentationRow).where(
        FirstContactPresentationRow.id == row.id,
        FirstContactPresentationRow.current_attempt_id == attempt.id,
        FirstContactPresentationRow.revision == revision,
    ).values(**values).execution_options(synchronize_session=False))
    _require(result.rowcount == 1, "FIRST_CONTACT_CAS_CONFLICT")
    session.refresh(row)
    attempt.state, attempt.updated_at = state, stamp
    if state == "CONFIRMED":
        attempt.confirmed_at = stamp
    _audit(session, row, attempt, event_type or state.lower())
    session.flush()
    return True


def record_generated(session: Session, token: ReservationToken, decision_id: str,
                     text_hash: str) -> bool:
    row, attempt = _with_token(session, token)
    decision = session.get(AgentDecisionRow, decision_id)
    _require(decision is not None and decision.event_id == attempt.event_id
             and decision.actor_binding_id is None and decision.audience == "unknown",
             "FIRST_CONTACT_DECISION_MISMATCH")
    scope = FirstContactScope(**{name: getattr(row, name) for name in FirstContactScope.__dataclass_fields__})
    event = _event(session, attempt.event_id, scope)
    _require(decision.interaction_id == event.interaction_id and bool(decision.proposed_response),
             "FIRST_CONTACT_DECISION_MISMATCH")
    _require(isinstance(text_hash, str) and bool(_HASH.fullmatch(text_hash))
             and hashlib.sha256(decision.proposed_response.encode("utf-8")).hexdigest() == text_hash,
             "FIRST_CONTACT_TEXT_MISMATCH")
    if attempt.generated_at is not None:
        _require(attempt.decision_id == decision_id and attempt.text_hash == text_hash,
                 "FIRST_CONTACT_GENERATION_CONFLICT")
        return False
    _require(attempt.state == "RESERVED", "FIRST_CONTACT_GENERATION_STATE_INVALID")
    attempt.decision_id, attempt.text_hash, attempt.generated_at = decision_id, text_hash, now_utc()
    return _transition(session, row, attempt, "RESERVED", event_type="generated")


def _linked(session: Session, attempt: FirstContactAttemptRow, intent_id: str, outbox_id: str):
    scope = session.get(FirstContactPresentationRow, attempt.presentation_id)
    decision = session.get(AgentDecisionRow, attempt.decision_id)
    intent = session.get(AgentExecutionIntentRow, intent_id)
    outbox = session.get(OutboxMessageRow, outbox_id)
    event = session.get(InboundEventRow, attempt.event_id)
    interaction = session.get(InteractionRow, event.interaction_id) if event else None
    _require(scope is not None and interaction is not None and event is not None
             and event.tenant_id == scope.tenant_id and interaction.tenant_id == scope.tenant_id
             and event.source == scope.source and (event.payload or {}).get("channel") == scope.channel
             and ((event.payload or {}).get("metadata") or {}).get("source_account") == scope.source_account
             and canonical_peer_hash(event) == scope.canonical_peer_hash,
             "FIRST_CONTACT_EFFECT_SCOPE_MISMATCH")
    _require(decision is not None and intent is not None and outbox is not None
             and decision.event_id == event.id and decision.interaction_id == event.interaction_id
             and decision.actor_binding_id is None and decision.audience == "unknown"
             and intent.agent_decision_id == decision.id and outbox.execution_intent_id == intent.id
             and outbox.interaction_id == event.interaction_id, "FIRST_CONTACT_EFFECT_MISMATCH")
    review = session.get(AgentResponseReviewRow, intent.response_review_id) if intent.response_review_id else None
    # Current release/review permission belongs to the existing execution gate.
    # Historical delivery remains provable after a subsequent review revocation.
    _require(intent.authorization_source == "POLICY_AUTONOMY" or (
        intent.authorization_source == "HUMAN_REVIEW" and review is not None
        and review.agent_decision_id == decision.id
    ), "FIRST_CONTACT_EFFECT_AUTHORIZATION_MISMATCH")
    _require(outbox.action_type == "agent_execution_text" and outbox.destination == "local_transport"
             and (outbox.payload or {}).get("message_type") == "text"
             and outbox.idempotency_key == "execution:" + intent.id,
             "FIRST_CONTACT_OUTBOX_MISMATCH")
    direct = direct_conversation_eligibility(event)
    _require(direct.eligible and (outbox.payload or {}).get("external_actor_id") == direct.peer_reference,
             "FIRST_CONTACT_RECIPIENT_MISMATCH")
    _require(isinstance(intent.effective_response_snapshot, str)
             and isinstance((outbox.payload or {}).get("text"), str)
             and attempt.text_hash == text_hash(intent.effective_response_snapshot)
             and attempt.text_hash == text_hash((outbox.payload or {}).get("text") or ""),
             "FIRST_CONTACT_TEXT_MISMATCH")
    return intent, outbox


def bind_outbox(session: Session, decision_id: str, intent_id: str, outbox_id: str) -> bool:
    session.flush()
    attempt_id = session.scalar(select(FirstContactAttemptRow.id).where(
        FirstContactAttemptRow.decision_id == decision_id,
    ))
    if attempt_id is None:
        return False
    row, attempt = _locked(session, attempt_id)
    _, outbox = _linked(session, attempt, intent_id, outbox_id)
    if attempt.outbox_id is not None:
        _require(attempt.execution_intent_id == intent_id and attempt.outbox_id == outbox_id
                 and attempt.idempotency_key == outbox.idempotency_key,
                 "FIRST_CONTACT_BINDING_CONFLICT")
        return False
    _require(attempt.state == "RESERVED" and attempt.generated_at is not None,
             "FIRST_CONTACT_BINDING_STATE_INVALID")
    attempt.execution_intent_id, attempt.outbox_id = intent_id, outbox_id
    attempt.idempotency_key, attempt.queued_at = outbox.idempotency_key, now_utc()
    return _transition(session, row, attempt, "QUEUED")


def _for_outbox(session: Session, outbox_id: str, *, lock: bool = True):
    attempt = session.scalar(select(FirstContactAttemptRow).where(
        FirstContactAttemptRow.outbox_id == outbox_id,
    ).execution_options(populate_existing=True))
    if attempt is None:
        return None
    if lock:
        return _locked(session, attempt.id)
    row = session.get(FirstContactPresentationRow, attempt.presentation_id, populate_existing=True)
    _require(row is not None and row.current_attempt_id == attempt.id,
             "FIRST_CONTACT_STALE_ATTEMPT")
    return row, attempt


def mark_sending(session: Session, outbox_id: str) -> bool:
    found = _for_outbox(session, outbox_id)
    if found is None:
        return False
    row, attempt = found
    _, outbox = _linked(session, attempt, attempt.execution_intent_id, outbox_id)
    if attempt.state == "SENDING":
        return False
    _require(attempt.state == "QUEUED", "FIRST_CONTACT_SEND_STATE_INVALID")
    _require(outbox.status in {"PROCESSING", "SENDING"}, "FIRST_CONTACT_OUTBOX_NOT_CLAIMED")
    attempt.sending_at = now_utc()
    return _transition(session, row, attempt, "SENDING")


def fail_before_effect(session: Session, token: ReservationToken, reason: str) -> bool:
    """Generation/cancellation failure with no linked external effect; never a TTL reset."""
    row, attempt = _with_token(session, token)
    _require(attempt.outbox_id is None and attempt.execution_intent_id is None,
             "FIRST_CONTACT_EFFECT_ALREADY_BOUND")
    if attempt.state == "FAILED":
        return False
    _require(attempt.state == "RESERVED", "FIRST_CONTACT_FAILURE_STATE_INVALID")
    _require(isinstance(reason, str) and bool(re.fullmatch(r"[A-Z][A-Z0-9_]{0,159}", reason)),
             "FIRST_CONTACT_FAILURE_REASON_INVALID")
    attempt.failure_reason = reason
    return _transition(session, row, attempt, "FAILED")


def observe_outbox(session: Session, outbox_id: str, *, known_before_effect: bool = False) -> bool:
    """Observe committed local state. DONE/SENT never implies device confirmation."""
    found = _for_outbox(session, outbox_id)
    if found is None:
        return False
    row, attempt = found
    _, outbox = _linked(session, attempt, attempt.execution_intent_id, outbox_id)
    if attempt.state == "CONFIRMED":
        return False
    if outbox.status == "DONE":
        if attempt.state == "SENT":
            return False
        _require(attempt.state in {"QUEUED", "SENDING", "AMBIGUOUS"},
                 "FIRST_CONTACT_OUTCOME_STATE_INVALID")
        attempt.sent_at = attempt.sent_at or now_utc()
        return _transition(session, row, attempt, "SENT")
    if outbox.status in {"FAILED", "BLOCKED", "CANCELLED", "CANCELED"}:
        safe = known_before_effect or (outbox.attempt_count == 0 and attempt.state == "QUEUED")
        state = "FAILED" if safe and attempt.message_reference is None and attempt.state != "SENT" else "AMBIGUOUS"
    elif outbox.status in {"AMBIGUOUS", "RECONCILIATION_REQUIRED"}:
        state = "AMBIGUOUS"
    else:
        return False
    if attempt.state == state:
        return False
    return _transition(session, row, attempt, state)


def delivery_scope(session: Session, outbox_id: str) -> dict | None:
    found = _for_outbox(session, outbox_id, lock=False)
    if found is None:
        return None
    row, attempt = found
    _linked(session, attempt, attempt.execution_intent_id, outbox_id)
    return {
        "schema_version": "1", "contract_version": PRESENTATION_VERSION,
        "attempt_id": attempt.id, "presentation_version": attempt.presentation_version,
        "tenant_id": row.tenant_id, "source": row.source, "channel": row.channel,
        "source_account": row.source_account, "canonical_peer_hash": row.canonical_peer_hash,
        "execution_intent_id": attempt.execution_intent_id, "outbox_id": attempt.outbox_id,
        "idempotency_key": attempt.idempotency_key, "text_hash": attempt.text_hash,
    }


def confirm_delivery(session: Session, expected_outbox_id: str, proof) -> bool:
    """Only the adapter's authenticated receipt may confirm this exact effect."""
    from attention_router.adapters.first_contact_delivery import VerifiedDeliveryProof

    _require(isinstance(proof, VerifiedDeliveryProof), "FIRST_CONTACT_AUTHENTICATED_PROOF_REQUIRED")
    found = _for_outbox(session, expected_outbox_id)
    _require(found is not None, "FIRST_CONTACT_OUTBOX_UNKNOWN")
    row, attempt = found
    expected = delivery_scope(session, expected_outbox_id)
    _require(all(getattr(proof, name, None) == value for name, value in expected.items()),
             "FIRST_CONTACT_PROOF_SCOPE_MISMATCH")
    if proof.status != "SENT":
        return False
    _require(isinstance(proof.message_reference, str) and 0 < len(proof.message_reference) <= 512,
             "FIRST_CONTACT_REFERENCE_REQUIRED")
    _require(attempt.message_reference in {None, proof.message_reference},
             "FIRST_CONTACT_REFERENCE_MISMATCH")
    _require(attempt.state in {"QUEUED", "SENDING", "SENT", "AMBIGUOUS", "CONFIRMED"},
             "FIRST_CONTACT_PROOF_STATE_INVALID")
    attempt.message_reference = proof.message_reference
    if attempt.proof_sha256 is None or (
        proof.ack is not None and (attempt.ack is None or proof.ack > attempt.ack)
    ):
        attempt.proof_sha256 = proof.body_sha256
    if proof.ack is not None:
        _require(type(proof.ack) is int and 0 <= proof.ack <= 4, "FIRST_CONTACT_ACK_INVALID")
        if attempt.ack is None or proof.ack > attempt.ack:
            ack_at = proof.ack_at
            if isinstance(ack_at, str):
                ack_at = datetime.fromisoformat(ack_at.replace("Z", "+00:00"))
            _require(ack_at is None or isinstance(ack_at, datetime), "FIRST_CONTACT_ACK_TIME_INVALID")
            _require(proof.ack < 2 or ack_at is not None, "FIRST_CONTACT_ACK_TIME_REQUIRED")
            attempt.ack = proof.ack
            attempt.ack_at = _utc(ack_at) if ack_at is not None else None
    if attempt.state == "CONFIRMED":
        session.flush()
        return False
    if proof.ack is not None and proof.ack >= 2:
        return _transition(session, row, attempt, "CONFIRMED")
    if attempt.state != "SENT":
        attempt.sent_at = attempt.sent_at or now_utc()
        _transition(session, row, attempt, "SENT")
    else:
        session.flush()
    return False
