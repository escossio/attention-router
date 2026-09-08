from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from attention_router.application.direct_conversation import direct_conversation_eligibility

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from attention_router.application.decision_pipeline import (
    DecisionRoutingContext,
    resolve_decision_routing,
)
from attention_router.application.owner_operational_control import (
    CONTROL_SOURCE_POLICY_DEFAULT,
    GLOBAL_GRACE_CONTRACT_VERSION,
    lock_owner_control_scope,
    resolve_global_owner_reply_grace_control,
)
from attention_router.domain.enums import InteractionState
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    AgentDecisionRow,
    AgentExecutionIntentRow,
    ConversationResponseGraceInboundRow,
    ConversationResponseGraceWindowRow,
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
    QueueRow,
)
from attention_router.infrastructure.repository import audit, create_inbound_event


CANARY_ACTOR_ID = "actor_sr_morgan_example"
CANARY_BINDING_ID = "ebc3ad9b-1c62-4030-b171-d6849d9affc9"
CANARY_AUDIENCE = "autonomy_canary"
CANARY_POLICY_ID = "morgan_presence_autonomy_v1"
GRACE_MODE = "TRAILING_EDGE"
OWNER_MANUAL_OUTBOUND_OBSERVED = "OWNER_MANUAL_OUTBOUND_OBSERVED"
ROUTER_AUTOMATED_OUTBOUND_OBSERVED = "ROUTER_AUTOMATED_OUTBOUND_OBSERVED"
UNKNOWN_FROM_ME = "UNKNOWN_FROM_ME"
OWNER_OBSERVATION_TYPES = {
    OWNER_MANUAL_OUTBOUND_OBSERVED,
    ROUTER_AUTOMATED_OUTBOUND_OBSERVED,
    UNKNOWN_FROM_ME,
}


@dataclass(frozen=True, slots=True)
class GraceEligibility:
    eligible: bool
    enabled: bool = False
    seconds: int = 0
    represented_owner_actor_key: str | None = None
    control_id: str | None = None
    control_revision: int | None = None
    control_source: str = CONTROL_SOURCE_POLICY_DEFAULT
    reason: str = "POLICY_NOT_CONFIGURED"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _event_occurred_at(event: InboundEventRow) -> datetime:
    raw = (event.payload or {}).get("occurred_at")
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return _utc(event.received_at)


def _event_causal_key(event: InboundEventRow) -> tuple[datetime, datetime]:
    received_at = _utc(event.received_at)
    return _event_occurred_at(event), received_at


def _lineage_causal_reference(
    session: Session,
    window: ConversationResponseGraceWindowRow,
) -> tuple[datetime, datetime] | None:
    events = session.scalars(
        select(InboundEventRow)
        .join(
            ConversationResponseGraceInboundRow,
            ConversationResponseGraceInboundRow.inbound_event_id == InboundEventRow.id,
        )
        .where(ConversationResponseGraceInboundRow.grace_window_id == window.id)
    ).all()
    return max((_event_causal_key(event) for event in events), default=None)


def _conversation_metadata(event: InboundEventRow) -> tuple[str | None, str, str, str]:
    payload = event.payload or {}
    metadata = payload.get("metadata") or {}
    return (
        metadata.get("conversation_key"),
        str(metadata.get("conversation_state") or "AMBIGUOUS"),
        str(metadata.get("source_account") or "default"),
        str(payload.get("channel") or "whatsapp"),
    )


def _lock_conversation(
    session: Session,
    *,
    tenant_id: str,
    source: str,
    source_account: str,
    conversation_key: str,
) -> None:
    """Serialize row creation and observation when no OPEN row exists yet."""
    if not session.bind or session.bind.dialect.name != "postgresql":
        return
    digest = stable_hash(f"{tenant_id}:{source}:{source_account}:{conversation_key}")
    lock_key = int(digest[:16], 16)
    if lock_key >= 2**63:
        lock_key -= 2**64
    session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})


def _manual_observation_at_or_after(
    session: Session,
    *,
    event: InboundEventRow,
    conversation_key: str,
    source_account: str,
) -> InboundEventRow | None:
    candidates = session.scalars(
        select(InboundEventRow)
        .where(
            InboundEventRow.tenant_id == event.tenant_id,
            InboundEventRow.source == event.source,
            InboundEventRow.payload["event_origin"].as_string()
            == OWNER_MANUAL_OUTBOUND_OBSERVED,
            InboundEventRow.payload["metadata"]["conversation_key"].as_string()
            == conversation_key,
            InboundEventRow.payload["metadata"]["source_account"].as_string()
            == source_account,
        )
        .order_by(InboundEventRow.received_at.desc())
    ).all()
    inbound_key = _event_causal_key(event)
    eligible = [candidate for candidate in candidates if _event_causal_key(candidate) >= inbound_key]
    return max(eligible, key=_event_causal_key, default=None)


def grace_eligibility(
    session: Session,
    routing: DecisionRoutingContext,
    *,
    event: InboundEventRow | None = None,
    lock_control: bool = False,
) -> GraceEligibility:
    direct = direct_conversation_eligibility(event)
    if not direct.eligible:
        return GraceEligibility(False, reason=direct.reason_code)
    if routing.policy_version is None:
        return GraceEligibility(False, reason="POLICY_VERSION_MISSING")
    represented_owner_actor_key = (
        routing.represented_subject.entity_id if routing.represented_subject else None
    )
    if not represented_owner_actor_key:
        return GraceEligibility(False, reason="REPRESENTED_OWNER_UNRESOLVED")
    if lock_control:
        lock_owner_control_scope(
            session,
            tenant_id=event.tenant_id,
            represented_owner_actor_key=represented_owner_actor_key,
            policy_id=None,
        )
    control = resolve_global_owner_reply_grace_control(
        session,
        tenant_id=event.tenant_id,
        represented_owner_actor_key=represented_owner_actor_key,
    )
    return GraceEligibility(
        True,
        enabled=control.enabled,
        seconds=(
            control.effective_seconds
            if control.effective_seconds is not None
            else 0
        ),
        represented_owner_actor_key=represented_owner_actor_key,
        control_id=control.control_id,
        control_revision=control.control_revision,
        control_source=control.source,
        reason=control.reason_code,
    )


def _window_query(
    *, tenant_id: str, source: str, source_account: str, conversation_key: str
):
    return select(ConversationResponseGraceWindowRow).where(
        ConversationResponseGraceWindowRow.tenant_id == tenant_id,
        ConversationResponseGraceWindowRow.source == source,
        ConversationResponseGraceWindowRow.source_account == source_account,
        ConversationResponseGraceWindowRow.conversation_key == conversation_key,
        ConversationResponseGraceWindowRow.state == "OPEN",
    )


def defer_decision_for_grace(
    session: Session,
    event: InboundEventRow,
    interaction: InteractionRow,
) -> bool:
    """Open or extend the owner's direct-conversation Grace before decision enqueue."""
    routing = resolve_decision_routing(session, event, interaction)
    eligibility = grace_eligibility(session, routing, event=event, lock_control=True)
    if not eligibility.eligible:
        direct = direct_conversation_eligibility(event)
        if direct.eligible or direct.reason_code in {"DIRECT_CONVERSATION_UNRESOLVED", "DIRECT_CONVERSATION_KEY_INVALID"}:
            interaction.state = InteractionState.WAITING.value
            audit(session, interaction.id, "grace.blocked", {"reason": eligibility.reason})
            return True
        return False
    conversation_key, conversation_state, source_account, channel = _conversation_metadata(event)
    if conversation_state != "READY" or not conversation_key:
        interaction.state = InteractionState.WAITING.value
        interaction.updated_at = now_utc()
        audit(
            session,
            interaction.id,
            "conversation_identity.ambiguous",
            {"reason": "GRACE_CONVERSATION_IDENTITY_UNRESOLVED"},
            event.correlation_id,
            event.id,
            policy_version_id=routing.policy_version.id,
            origin="owner_reply_grace",
        )
        return True

    _lock_conversation(
        session,
        tenant_id=interaction.tenant_id,
        source=event.source,
        source_account=source_account,
        conversation_key=conversation_key,
    )
    prior_owner_reply = _manual_observation_at_or_after(
        session,
        event=event,
        conversation_key=conversation_key,
        source_account=source_account,
    )
    if prior_owner_reply is not None:
        interaction.state = InteractionState.CANCELED_BY_HUMAN_REPLY.value
        interaction.updated_at = now_utc()
        audit(
            session,
            interaction.id,
            "grace.inbound_preempted_by_owner_reply",
            {
                "reason": "OWNER_REPLY_ALREADY_OBSERVED",
                "owner_observation_event_id": prior_owner_reply.id,
                "conversation_key_hash": stable_hash(conversation_key),
            },
            event.correlation_id,
            event.id,
            policy_version_id=routing.policy_version.id,
            origin="owner_reply_grace",
        )
        return True

    prior_membership = session.scalar(
        select(ConversationResponseGraceInboundRow).where(
            ConversationResponseGraceInboundRow.inbound_event_id == event.id
        )
    )
    if prior_membership:
        return True

    query = _window_query(
        tenant_id=interaction.tenant_id,
        source=event.source,
        source_account=source_account,
        conversation_key=conversation_key,
    )
    if session.bind and session.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    window = session.scalar(query)
    stamp = _utc(event.received_at)
    if window is None:
        window = ConversationResponseGraceWindowRow(
            id=new_id(),
            tenant_id=interaction.tenant_id,
            source=event.source,
            source_account=source_account,
            channel=channel,
            conversation_key=conversation_key,
            actor_id=routing.binding.actor_key if routing.binding else interaction.contact_id,
            actor_binding_id=routing.binding.id if routing.binding else None,
            audience=routing.audience,
            policy_version_id=routing.policy_version.id,
            represented_owner_actor_key=eligibility.represented_owner_actor_key,
            operational_control_id=eligibility.control_id,
            operational_control_revision=eligibility.control_revision,
            operational_control_source=eligibility.control_source,
            auto_release_enabled=eligibility.enabled,
            effective_grace_seconds=eligibility.seconds,
            state="OPEN",
            generation=1,
            opened_at=stamp,
            last_inbound_at=stamp,
            due_at=stamp + timedelta(seconds=eligibility.seconds),
            anchor_event_id=event.id,
            anchor_interaction_id=interaction.id,
            provenance={
                "grace_contract_version": GLOBAL_GRACE_CONTRACT_VERSION,
                "mode": GRACE_MODE,
                "seconds": eligibility.seconds,
                "enabled": eligibility.enabled,
                "control_source": eligibility.control_source,
                "control_revision": eligibility.control_revision,
            },
            created_at=stamp,
            updated_at=stamp,
        )
        try:
            with session.begin_nested():
                session.add(window)
                session.flush()
        except IntegrityError:
            window = session.scalar(query)
            if window is None:
                raise
        event_type = "grace.opened"
    else:
        window.generation += 1
        # Every distinct membership advances generation, including late arrivals.
        # Receipt time remains the existing trailing-edge clock; older events
        # must not displace the anchor or shorten a previously established wait.
        if stamp >= _utc(window.last_inbound_at):
            window.last_inbound_at = stamp
            window.anchor_event_id = event.id
            window.anchor_interaction_id = interaction.id
        window.due_at = max(
            _utc(window.due_at),
            _utc(window.last_inbound_at) + timedelta(seconds=eligibility.seconds),
        )
        window.policy_version_id = routing.policy_version.id
        window.represented_owner_actor_key = eligibility.represented_owner_actor_key
        window.operational_control_id = eligibility.control_id
        window.operational_control_revision = eligibility.control_revision
        window.operational_control_source = eligibility.control_source
        window.auto_release_enabled = eligibility.enabled
        window.effective_grace_seconds = eligibility.seconds
        window.claimed_at = None
        window.claimed_by = None
        window.claimed_generation = None
        window.updated_at = stamp
        event_type = "grace.extended"

    membership = ConversationResponseGraceInboundRow(
        id=new_id(),
        grace_window_id=window.id,
        inbound_event_id=event.id,
        interaction_id=interaction.id,
        generation=window.generation,
        associated_at=stamp,
    )
    session.add(membership)
    interaction.state = InteractionState.WAITING.value
    interaction.policy_id = routing.policy_version.policy_id
    interaction.policy_version_id = routing.policy_version.id
    interaction.updated_at = stamp
    audit(
        session,
        interaction.id,
        event_type,
        {
            "grace_window_id": window.id,
            "generation": window.generation,
            "due_at": window.due_at.isoformat(),
            "auto_release_enabled": eligibility.enabled,
            "effective_grace_seconds": eligibility.seconds,
            "control_source": eligibility.control_source,
            "control_revision": eligibility.control_revision,
            "conversation_key_hash": stable_hash(conversation_key),
        },
        event.correlation_id,
        event.id,
        policy_version_id=routing.policy_version.id,
        origin="owner_reply_grace",
    )
    session.flush()
    return True


def _routing_still_valid(
    session: Session,
    window: ConversationResponseGraceWindowRow,
    event: InboundEventRow,
    interaction: InteractionRow,
) -> bool:
    routing = resolve_decision_routing(session, event, interaction)
    eligibility = grace_eligibility(session, routing, event=event)
    direct = direct_conversation_eligibility(event)
    return bool(
        eligibility.eligible
        and routing.policy_version
        and routing.policy_version.id == window.policy_version_id
        and eligibility.enabled
        and direct.conversation_key == window.conversation_key
        and direct.source_account == window.source_account
        and event.source == window.source
        and eligibility.control_revision == window.operational_control_revision
        and eligibility.seconds == window.effective_grace_seconds
        and routing.represented_subject
        and routing.represented_subject.entity_id == window.represented_owner_actor_key
    )


def release_grace_window(
    session: Session,
    window: ConversationResponseGraceWindowRow,
    *,
    expected_generation: int,
    worker: str,
    timestamp: datetime | None = None,
) -> bool:
    stamp = _utc(timestamp or now_utc())
    if (
        window.state != "OPEN"
        or not window.auto_release_enabled
        or window.generation != expected_generation
        or _utc(window.due_at) > stamp
    ):
        return False
    event = session.get(InboundEventRow, window.anchor_event_id)
    interaction = session.get(InteractionRow, window.anchor_interaction_id)
    if event is None or interaction is None or not _routing_still_valid(session, window, event, interaction):
        window.state = "CANCELED"
        window.canceled_at = stamp
        window.cancellation_reason = "GRACE_REVALIDATION_FAILED"
        window.updated_at = stamp
        audit(
            session,
            interaction.id if interaction else None,
            "grace.canceled",
            {"grace_window_id": window.id, "reason": "GRACE_REVALIDATION_FAILED"},
            origin="owner_reply_grace",
            tenant_id=window.tenant_id,
        )
        return False
    window.claimed_at = stamp
    window.claimed_by = worker
    window.claimed_generation = expected_generation
    audit(
        session,
        interaction.id,
        "grace.claimed",
        {"grace_window_id": window.id, "generation": expected_generation},
        event.correlation_id,
        event.id,
        policy_version_id=window.policy_version_id,
        origin="owner_reply_grace",
    )
    queue_id = f"decision:{event.id}"
    existing = session.get(QueueRow, queue_id)
    if existing is None:
        session.add(QueueRow(
            id=queue_id,
            kind="decision",
            payload={
                "event_id": event.id,
                "interaction_id": interaction.id,
                "grace_window_id": window.id,
                "grace_generation": expected_generation,
            },
            status="PENDING",
            created_at=stamp,
        ))
    window.state = "RELEASED"
    window.released_at = stamp
    window.updated_at = stamp
    audit(
        session,
        interaction.id,
        "grace.released",
        {"grace_window_id": window.id, "generation": expected_generation, "queue_id": queue_id},
        event.correlation_id,
        event.id,
        policy_version_id=window.policy_version_id,
        origin="owner_reply_grace",
    )
    return True


def process_due_grace_windows(
    session: Session,
    worker: str,
    *,
    timestamp: datetime | None = None,
    limit: int = 10,
) -> int:
    stamp = _utc(timestamp or now_utc())
    query = (
        select(ConversationResponseGraceWindowRow)
        .where(
            ConversationResponseGraceWindowRow.state == "OPEN",
            ConversationResponseGraceWindowRow.auto_release_enabled.is_(True),
            ConversationResponseGraceWindowRow.due_at <= stamp,
        )
        .order_by(ConversationResponseGraceWindowRow.due_at, ConversationResponseGraceWindowRow.id)
        .limit(limit)
    )
    if session.bind and session.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    count = 0
    for window in session.scalars(query).all():
        if release_grace_window(
            session,
            window,
            expected_generation=window.generation,
            worker=worker,
            timestamp=stamp,
        ):
            count += 1
    session.flush()
    return count


def grace_window_for_interaction(
    session: Session,
    interaction_id: str,
    *,
    lock: bool = False,
) -> ConversationResponseGraceWindowRow | None:
    query = (
        select(ConversationResponseGraceWindowRow)
        .join(
            ConversationResponseGraceInboundRow,
            ConversationResponseGraceInboundRow.grace_window_id == ConversationResponseGraceWindowRow.id,
        )
        .where(ConversationResponseGraceInboundRow.interaction_id == interaction_id)
        .order_by(ConversationResponseGraceInboundRow.associated_at.desc())
        .limit(1)
    )
    if lock and session.bind and session.bind.dialect.name == "postgresql":
        query = query.with_for_update(of=ConversationResponseGraceWindowRow)
    return session.scalar(query.execution_options(populate_existing=True))


def grace_allows_interaction(session: Session, interaction_id: str, *, lock: bool = False) -> bool:
    window = grace_window_for_interaction(session, interaction_id, lock=lock)
    return window is None or window.state == "RELEASED"


def _cancel_window(
    session: Session,
    window: ConversationResponseGraceWindowRow,
    observation: InboundEventRow,
) -> tuple[bool, bool]:
    memberships = session.scalars(
        select(ConversationResponseGraceInboundRow).where(
            ConversationResponseGraceInboundRow.grace_window_id == window.id
        )
    ).all()
    interaction_ids = [item.interaction_id for item in memberships]
    decisions = session.scalars(
        select(AgentDecisionRow).where(AgentDecisionRow.interaction_id.in_(interaction_ids))
    ).all() if interaction_ids else []
    decision_ids = [item.id for item in decisions]
    intents = session.scalars(
        select(AgentExecutionIntentRow).where(
            AgentExecutionIntentRow.agent_decision_id.in_(decision_ids)
        )
    ).all() if decision_ids else []
    intent_ids = [item.id for item in intents]
    outbox_query = select(OutboxMessageRow).where(
        OutboxMessageRow.execution_intent_id.in_(intent_ids)
    ) if intent_ids else None
    if outbox_query is not None and session.bind and session.bind.dialect.name == "postgresql":
        outbox_query = outbox_query.with_for_update()
    outboxes = session.scalars(outbox_query).all() if outbox_query is not None else []
    irreversible = any(row.status in {"PROCESSING", "DONE", "AMBIGUOUS", "FAILED"} for row in outboxes)
    irreversible = irreversible or any(intent.status == "SENT" for intent in intents)
    anchor = session.get(InteractionRow, window.anchor_interaction_id)
    if irreversible:
        audit(
            session,
            anchor.id if anchor else None,
            "human_cancel.too_late",
            {"grace_window_id": window.id, "reason": "OUTBOX_IRREVERSIBLE"},
            observation.correlation_id,
            observation.id,
            policy_version_id=window.policy_version_id,
            origin="owner_reply_grace",
            tenant_id=window.tenant_id,
        )
        return False, True
    stamp = now_utc()
    window.state = "CANCELED"
    window.canceled_at = stamp
    window.canceled_by_event_id = observation.id
    window.cancellation_reason = "OWNER_MANUAL_REPLY"
    window.updated_at = stamp
    for interaction_id in interaction_ids:
        interaction = session.get(InteractionRow, interaction_id)
        if interaction:
            interaction.state = InteractionState.CANCELED_BY_HUMAN_REPLY.value
            interaction.updated_at = stamp
    queue_rows = session.scalars(
        select(QueueRow).where(
            QueueRow.payload["grace_window_id"].as_string() == window.id,
            QueueRow.status.in_(["PENDING", "pending", "WAITING_TRANSCRIPTION"]),
        )
    ).all()
    for queue in queue_rows:
        queue.status = "CANCELED"
        queue.processed_at = stamp
    for intent in intents:
        if intent.status != "SENT":
            intent.status = "CANCELLED"
            intent.blocked_reason = "CANCELED_BY_HUMAN_REPLY"
    for outbox in outboxes:
        if outbox.status == "PENDING":
            outbox.status = "CANCELED"
            outbox.completed_at = stamp
            outbox.last_error = "CANCELED_BY_HUMAN_REPLY"
            if outbox.action_type == "agent_execution_voice":
                from attention_router.application.voice_tts import mark_voice_outbox_terminal

                mark_voice_outbox_terminal(session, outbox)
    audit(
        session,
        anchor.id if anchor else None,
        "grace.canceled",
        {"grace_window_id": window.id, "reason": "OWNER_MANUAL_REPLY"},
        observation.correlation_id,
        observation.id,
        policy_version_id=window.policy_version_id,
        origin="owner_reply_grace",
        tenant_id=window.tenant_id,
    )
    return True, False


def receive_owner_outbound_observation(session: Session, event: Any) -> dict[str, Any]:
    payload = event.normalized_payload
    existing = session.scalar(
        select(InboundEventRow).where(
            InboundEventRow.tenant_id == event.tenant_id,
            InboundEventRow.source == event.source,
            InboundEventRow.external_event_id == event.external_event_id,
        )
    )
    if existing:
        if existing.payload_hash != stable_hash(payload):
            raise ValueError("OWNER_OBSERVATION_PAYLOAD_CONFLICT")
        return {
            "receipt_id": existing.id,
            "interaction_id": None,
            "status": existing.status,
            "correlation_id": existing.correlation_id,
        }
    try:
        with session.begin_nested():
            receipt = create_inbound_event(
                session,
                event.source,
                event.external_event_id,
                event.event_type,
                payload,
                event.correlation_id,
                event.tenant_id,
                event.received_at,
            )
    except IntegrityError:
        receipt = session.scalar(
            select(InboundEventRow).where(
                InboundEventRow.tenant_id == event.tenant_id,
                InboundEventRow.source == event.source,
                InboundEventRow.external_event_id == event.external_event_id,
            )
        )
        if receipt is None:
            raise
        if receipt.payload_hash != stable_hash(payload):
            raise ValueError("OWNER_OBSERVATION_PAYLOAD_CONFLICT")
        return {
            "receipt_id": receipt.id,
            "interaction_id": None,
            "status": receipt.status,
            "correlation_id": receipt.correlation_id,
        }
    metadata = payload.get("metadata") or {}
    classification = event.event_origin
    event_type = {
        OWNER_MANUAL_OUTBOUND_OBSERVED: "owner_manual_outbound.observed",
        ROUTER_AUTOMATED_OUTBOUND_OBSERVED: "router_automated_outbound.observed",
        UNKNOWN_FROM_ME: "from_me.ambiguous",
    }[classification]
    audit(
        session,
        None,
        event_type,
        {"receipt_id": receipt.id, "classification": classification},
        receipt.correlation_id,
        receipt.id,
        origin="owner_reply_grace",
        tenant_id=event.tenant_id,
    )
    if classification == OWNER_MANUAL_OUTBOUND_OBSERVED:
        conversation_key = metadata.get("conversation_key")
        if metadata.get("conversation_state") != "READY" or not conversation_key:
            audit(
                session,
                None,
                "conversation_identity.ambiguous",
                {"receipt_id": receipt.id, "reason": "OWNER_OBSERVATION_IDENTITY_UNRESOLVED"},
                receipt.correlation_id,
                receipt.id,
                origin="owner_reply_grace",
                tenant_id=event.tenant_id,
            )
        else:
            source_account = str(metadata.get("source_account") or "default")
            _lock_conversation(
                session,
                tenant_id=event.tenant_id,
                source=event.source,
                source_account=source_account,
                conversation_key=conversation_key,
            )
            owner_causal_key = _event_causal_key(receipt)
            query = (
                select(ConversationResponseGraceWindowRow)
                .where(
                    ConversationResponseGraceWindowRow.tenant_id == event.tenant_id,
                    ConversationResponseGraceWindowRow.source == event.source,
                    ConversationResponseGraceWindowRow.source_account == source_account,
                    ConversationResponseGraceWindowRow.conversation_key == conversation_key,
                )
                # Find the barrier BEFORE testing time, state or reversibility.
                # Two rows suffice to detect a tied maximum without treating a
                # UUID as causal evidence. The conversation lock excludes inbound.
                .order_by(
                    ConversationResponseGraceWindowRow.last_inbound_at.desc(),
                    ConversationResponseGraceWindowRow.created_at.desc(),
                )
                .limit(2)
            )
            if session.bind and session.bind.dialect.name == "postgresql":
                query = query.with_for_update()
            windows = session.scalars(query.execution_options(populate_existing=True)).all()
            if windows:
                window = windows[0]
                lineage_causal_key = _lineage_causal_reference(session, window)
                ambiguous = len(windows) > 1 and (
                    _utc(window.last_inbound_at), _utc(window.created_at)
                ) == (
                    _utc(windows[1].last_inbound_at), _utc(windows[1].created_at)
                )
                if ambiguous:
                    no_op = "human_cancel.ambiguous_lineage"
                elif lineage_causal_key is None:
                    no_op = "human_cancel.causal_reference_unavailable"
                elif owner_causal_key < lineage_causal_key:
                    no_op = "human_cancel.stale_observation"
                elif window.state not in {"OPEN", "RELEASED"}:
                    no_op = "human_cancel.terminal_lineage"
                else:
                    no_op = None
                    _cancel_window(session, window, receipt)
                if no_op:
                    audit(
                        session,
                        None,
                        no_op,
                        {"conversation_key_hash": stable_hash(conversation_key)},
                        receipt.correlation_id,
                        receipt.id,
                        origin="owner_reply_grace",
                        tenant_id=event.tenant_id,
                    )
    receipt.status = "PROCESSED"
    receipt.processed_at = now_utc()
    session.flush()
    return {
        "receipt_id": receipt.id,
        "interaction_id": None,
        "status": receipt.status,
        "correlation_id": receipt.correlation_id,
    }
