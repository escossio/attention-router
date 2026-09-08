from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from attention_router.adapters.inbound import NormalizedInboundEvent
from attention_router.application import services
from attention_router.application.owner_reply_grace import (
    CANARY_ACTOR_ID,
    CANARY_AUDIENCE,
    CANARY_BINDING_ID,
    CANARY_POLICY_ID,
    OWNER_MANUAL_OUTBOUND_OBSERVED,
    ROUTER_AUTOMATED_OUTBOUND_OBSERVED,
    UNKNOWN_FROM_ME,
    process_due_grace_windows,
)
from attention_router.application.platform.entities import create_relationship
from attention_router.core.entities import EntityReference
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.enums import InteractionState
from attention_router.domain.models import now_utc
from attention_router.infrastructure.models import (
    ActorBindingRow,
    AgentDecisionRow,
    AgentExecutionIntentRow,
    AuditEventRow,
    ConversationResponseGraceInboundRow,
    ConversationResponseGraceWindowRow,
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
    PolicyRow,
    QueueRow,
)
from attention_router.infrastructure.repository import (
    ensure_policy_version,
    provision_morgan_owner_reply_grace_policy,
)


PEER_LID = "5500000000020@lid"
PEER_C_US = "5500000000019@c.us"
OWNER_ACTOR_ID = "actor_owner_test"


def _policy_config() -> dict:
    return {
        "identifier": CANARY_POLICY_ID,
        "name": "Morgan Presence Autonomy",
        "match_criteria": {"audience": CANARY_AUDIENCE, "binding_id": CANARY_BINDING_ID},
        "priority": 2000,
        "specificity": 2000,
        "tone": "cordial",
        "initial_wait_seconds": 1,
        "allowed_disclosures": ["availability_hint"],
        "allowed_actions": ["respond"],
        "escalation_steps": ["respond"],
        "ack_timeout_seconds": 30,
        "repetition_limit": 1,
        "cancellation_conditions": ["human_reply"],
        "completion_conditions": ["safe_completion"],
        "execution_mode": "AUTO_ALLOWED",
        "execution_scope": {
            "actor_ids": [CANARY_ACTOR_ID],
            "binding_ids": [CANARY_BINDING_ID],
            "audiences": [CANARY_AUDIENCE],
        },
    }


def _install_canary(session: Session) -> str:
    stamp = now_utc()
    session.add_all(
        [
            ActorBindingRow(
                id="owner-binding-test",
                tenant_id=DEFAULT_TENANT_ID,
                source="wwebjs",
                external_actor_id="owner@c.us",
                actor_key=OWNER_ACTOR_ID,
                display_name="Alex",
                actor_category="owner",
                active_context=None,
                is_active=True,
                binding_metadata={"owner": True},
                created_at=stamp,
                updated_at=stamp,
            ),
            ActorBindingRow(
                id=CANARY_BINDING_ID,
                tenant_id=DEFAULT_TENANT_ID,
                source="wwebjs",
                external_actor_id=PEER_LID,
                actor_key=CANARY_ACTOR_ID,
                display_name="Sr. Morgan Example",
                actor_category="family_core",
                active_context=None,
                is_active=True,
                binding_metadata={"audience": CANARY_AUDIENCE, "canary": True},
                created_at=stamp,
                updated_at=stamp,
            ),
        ]
    )
    config = _policy_config()
    policy = PolicyRow(
        **{key: config[key] for key in PolicyRow.__table__.columns.keys() if key in config},
        tenant_id=DEFAULT_TENANT_ID,
        is_active=True,
    )
    session.add(policy)
    session.flush()
    original = ensure_policy_version(session, policy, config, "test")
    create_relationship(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        source=EntityReference(entity_type="ACTOR", entity_id=CANARY_ACTOR_ID),
        target=EntityReference(entity_type="ACTOR", entity_id=OWNER_ACTOR_ID),
        relationship_type="pai",
        metadata_sanitized={"authorization": "explicit_human_authorization"},
    )
    current = provision_morgan_owner_reply_grace_policy(session, origin="test")
    session.flush()
    assert current.id != original.id
    return current.id


def _inbound(
    external_event_id: str,
    received_at: datetime,
    *,
    conversation_key: str = "wwebjs:5500000000020@lid",
    conversation_state: str = "READY",
) -> NormalizedInboundEvent:
    return NormalizedInboundEvent(
        source="wwebjs",
        external_event_id=external_event_id,
        event_type="message",
        received_at=received_at,
        occurred_at=received_at,
        actor_id=PEER_LID,
        actor_display_name="Sr. Morgan Example",
        actor_category="family_core",
        channel="whatsapp",
        content=f"Mensagem {external_event_id}",
        event_origin="EXTERNAL_INBOUND",
        metadata={
            "conversation_key": conversation_key,
            "conversation_state": conversation_state,
            "source_account": "default",
            "peer_identifiers": [PEER_LID, PEER_C_US],
            "from_me": False,
        },
    )


def _owner_observation(
    external_event_id: str,
    timestamp: datetime,
    *,
    classification: str = OWNER_MANUAL_OUTBOUND_OBSERVED,
    conversation_key: str = "wwebjs:5500000000020@lid",
    conversation_state: str = "READY",
) -> NormalizedInboundEvent:
    return NormalizedInboundEvent(
        source="wwebjs",
        external_event_id=external_event_id,
        event_type="message",
        received_at=timestamp,
        occurred_at=timestamp,
        actor_id=PEER_LID,
        actor_display_name="Owner outbound observation",
        actor_category="owner_outbound_observation",
        channel="whatsapp",
        content="[owner outbound observation]",
        event_origin=classification,
        owner_authenticated=False,
        metadata={
            "conversation_key": conversation_key,
            "conversation_state": conversation_state,
            "source_account": "default",
            "peer_identifiers": [PEER_LID, PEER_C_US],
            "from_me": True,
            "from_me_classification": classification,
        },
    )


def _with_timestamps(
    event: NormalizedInboundEvent,
    *,
    occurred_at: datetime | None,
    received_at: datetime,
) -> NormalizedInboundEvent:
    return event.model_copy(
        update={"occurred_at": occurred_at, "received_at": received_at}
    )


def _receive_canary(session: Session, event: NormalizedInboundEvent) -> dict:
    result = services.receive_normalized_inbound_event(session, event)
    session.flush()
    return result


def _window(session: Session) -> ConversationResponseGraceWindowRow:
    return session.scalar(select(ConversationResponseGraceWindowRow))


def _add_reversible_downstream(
    session: Session, *, outbox_status: str = "PENDING", window=None, blocked_without_outbox=False,
):
    window = window or _window(session)
    stamp = now_utc()
    decision = AgentDecisionRow(
        id=f"decision-{window.id}",
        event_id=window.anchor_event_id,
        interaction_id=window.anchor_interaction_id,
        agent_blueprint_id=None,
        agent_blueprint_version=None,
        actor_id=CANARY_ACTOR_ID,
        actor_binding_id=CANARY_BINDING_ID,
        audience=CANARY_AUDIENCE,
        policy_version_id=window.policy_version_id,
        decision_pipeline_version="v1",
        decision_type="RESPOND",
        recommended_action="respond",
        proposed_response="Test response",
        escalation_required=False,
        confidence=0.9,
        missing_information=[],
        execution_allowed=True,
        external_delivery_allowed=True,
        reasoning_summary="test",
        status="DRY_RUN",
        created_at=stamp,
    )
    intent = AgentExecutionIntentRow(
        id=f"intent-{window.id}",
        agent_decision_id=decision.id,
        response_review_id=None,
        autonomy_evaluation_id=None,
        authorization_source="POLICY_AUTONOMY",
        intent_type="WHATSAPP_RESPONSE",
        effective_response_snapshot="Test response",
        status="QUEUED",
        execution_allowed=True,
        external_delivery_allowed=True,
        blocked_reason=None,
        release_status="RELEASED",
        released_at=stamp,
        released_by="test",
        recipient_reference=PEER_LID,
        idempotency_key=f"autonomy:{window.id}:v1",
        created_at=stamp,
    )
    outbox = OutboxMessageRow(
        id=f"outbox-{window.id}",
        interaction_id=window.anchor_interaction_id,
        action_type="agent_execution_text",
        destination="local_transport",
        payload={"text": "Test response", "execution_intent_id": intent.id},
        status=outbox_status,
        created_at=stamp,
        available_at=stamp,
        attempt_count=1 if outbox_status == "PROCESSING" else 0,
        idempotency_key=f"execution:{window.id}",
        execution_intent_id=intent.id,
    )
    session.add(decision)
    session.flush()
    if blocked_without_outbox:
        intent.status = "BLOCKED"
        intent.blocked_reason = "AGENT_EXECUTION_DISABLED"
    session.add(intent)
    session.flush()
    if blocked_without_outbox:
        return intent, None
    session.add(outbox)
    session.flush()
    return intent, outbox


@pytest.fixture()
def canary(session: Session) -> Session:
    _install_canary(session)
    session.commit()
    return session


def test_required_01_eligible_inbound_opens_30_second_window(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    result = _receive_canary(canary, _inbound("m1", stamp))
    window = _window(canary)
    assert window.state == "OPEN"
    assert window.generation == 1
    assert window.due_at.replace(tzinfo=timezone.utc) == stamp + timedelta(seconds=30)
    assert window.anchor_interaction_id == result["id"]


def test_required_02_before_deadline_has_no_executable_downstream(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    result = _receive_canary(canary, _inbound("m1", stamp))
    assert canary.get(InteractionRow, result["id"]).state == InteractionState.WAITING.value
    assert canary.scalar(select(func.count()).select_from(QueueRow)) == 0
    assert canary.scalar(select(func.count()).select_from(AgentDecisionRow)) == 0
    assert canary.scalar(select(func.count()).select_from(AgentExecutionIntentRow)) == 0
    assert canary.scalar(select(func.count()).select_from(OutboxMessageRow)) == 0


def test_required_03_timeout_releases_exactly_one_decision(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    result = _receive_canary(canary, _inbound("m1", stamp))
    assert process_due_grace_windows(canary, "worker", timestamp=stamp + timedelta(seconds=29)) == 0
    assert process_due_grace_windows(canary, "worker", timestamp=stamp + timedelta(seconds=30)) == 1
    assert process_due_grace_windows(canary, "worker", timestamp=stamp + timedelta(seconds=31)) == 0
    queue = canary.scalar(select(QueueRow))
    assert queue.id == "decision:" + result["inbound_event_id"]
    assert queue.payload["grace_generation"] == 1
    assert _window(canary).state == "RELEASED"


@pytest.mark.parametrize("offset", [29, 1], ids=["t-plus-29", "t-plus-1"])
def test_required_04_05_manual_reply_inside_window_cancels(canary, offset):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    result = _receive_canary(canary, _inbound("m1", stamp))
    observation = _owner_observation("manual-1", stamp + timedelta(seconds=offset))
    observed = services.receive_normalized_inbound_event(canary, observation)
    assert observed["interaction_id"] is None
    assert _window(canary).state == "CANCELED"
    assert canary.get(InteractionRow, result["id"]).state == InteractionState.CANCELED_BY_HUMAN_REPLY.value
    assert process_due_grace_windows(canary, "worker", timestamp=stamp + timedelta(seconds=60)) == 0
    assert canary.scalar(select(func.count()).select_from(QueueRow)) == 0


def test_required_06_reply_after_release_cancels_pending_decision(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    _receive_canary(canary, _inbound("m1", stamp))
    process_due_grace_windows(canary, "worker", timestamp=stamp + timedelta(seconds=30))
    services.receive_normalized_inbound_event(
        canary, _owner_observation("manual-after-release", stamp + timedelta(seconds=31))
    )
    assert _window(canary).state == "CANCELED"
    assert canary.scalar(select(QueueRow)).status == "CANCELED"


def test_required_07_router_automated_observation_never_cancels(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    _receive_canary(canary, _inbound("m1", stamp))
    services.receive_normalized_inbound_event(
        canary,
        _owner_observation(
            "andy-create-1", stamp + timedelta(seconds=1),
            classification=ROUTER_AUTOMATED_OUTBOUND_OBSERVED,
        ),
    )
    assert _window(canary).state == "OPEN"


def test_failed_provenance_unknown_observation_does_not_cancel_open_grace(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    result = _receive_canary(canary, _inbound("m1", stamp))

    observed = services.receive_normalized_inbound_event(
        canary,
        _owner_observation(
            "failed-provenance-create-1",
            stamp + timedelta(seconds=1),
            classification=UNKNOWN_FROM_ME,
        ),
    )

    assert observed["interaction_id"] is None
    assert canary.scalar(
        select(func.count()).select_from(AuditEventRow).where(
            AuditEventRow.event_type == "from_me.ambiguous"
        )
    ) == 1
    assert _window(canary).state == "OPEN"
    assert canary.get(InteractionRow, result["id"]).state == InteractionState.WAITING.value
    assert canary.scalar(
        select(func.count()).select_from(InteractionRow).where(
            InteractionRow.state == InteractionState.CANCELED_BY_HUMAN_REPLY.value
        )
    ) == 0
    assert canary.scalar(
        select(func.count()).select_from(QueueRow).where(QueueRow.status == "CANCELED")
    ) == 0


def test_required_08_trailing_edge_coalesces_m1_and_m2(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    first = _receive_canary(canary, _inbound("m1", stamp))
    second = _receive_canary(canary, _inbound("m2", stamp + timedelta(seconds=10)))
    window = _window(canary)
    assert window.generation == 2
    assert window.due_at.replace(tzinfo=timezone.utc) == stamp + timedelta(seconds=40)
    assert window.anchor_interaction_id == second["id"]
    assert window.anchor_interaction_id != first["id"]
    assert canary.scalar(select(func.count()).select_from(ConversationResponseGraceInboundRow)) == 2
    assert process_due_grace_windows(canary, "worker", timestamp=stamp + timedelta(seconds=30)) == 0
    assert process_due_grace_windows(canary, "worker", timestamp=stamp + timedelta(seconds=40)) == 1
    assert canary.scalar(select(func.count()).select_from(QueueRow)) == 1


def test_required_09_restart_preserves_deadline(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    _receive_canary(canary, _inbound("m1", stamp))
    canary.commit()
    with Session(canary.bind, expire_on_commit=False) as restarted:
        window = restarted.scalar(select(ConversationResponseGraceWindowRow))
        assert window.state == "OPEN"
        assert window.due_at.replace(tzinfo=timezone.utc) == stamp + timedelta(seconds=30)
        assert process_due_grace_windows(
            restarted, "restarted-worker", timestamp=stamp + timedelta(seconds=30)
        ) == 1
        restarted.commit()
    canary.expire_all()
    assert canary.scalar(select(func.count()).select_from(QueueRow)) == 1


def test_required_10_duplicate_inbound_is_idempotent_without_extension(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    event = _inbound("m1", stamp)
    first = _receive_canary(canary, event)
    canary.commit()
    deadline = _window(canary).due_at
    duplicate = services.receive_normalized_inbound_event(canary, event)
    assert duplicate["id"] == first["id"]
    assert _window(canary).generation == 1
    assert _window(canary).due_at == deadline
    assert canary.scalar(select(func.count()).select_from(ConversationResponseGraceInboundRow)) == 1


def test_required_11_duplicate_owner_observation_is_idempotent(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    _receive_canary(canary, _inbound("m1", stamp))
    observation = _owner_observation("manual-1", stamp + timedelta(seconds=1))
    first = services.receive_normalized_inbound_event(canary, observation)
    canary.commit()
    second = services.receive_normalized_inbound_event(canary, observation)
    assert second == first
    assert canary.scalar(
        select(func.count()).select_from(InboundEventRow).where(
            InboundEventRow.external_event_id == "manual-1"
        )
    ) == 1


def test_required_12_alias_observation_uses_canonical_conversation_key(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    _receive_canary(canary, _inbound("m1", stamp))
    observation = _owner_observation("manual-c-us", stamp + timedelta(seconds=1))
    observation.actor_id = PEER_C_US
    services.receive_normalized_inbound_event(canary, observation)
    assert _window(canary).state == "CANCELED"


def test_required_13_other_conversation_does_not_cancel(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    _receive_canary(canary, _inbound("m1", stamp))
    services.receive_normalized_inbound_event(
        canary,
        _owner_observation(
            "manual-other", stamp + timedelta(seconds=1),
            conversation_key="wwebjs:other-peer",
        ),
    )
    assert _window(canary).state == "OPEN"


def test_required_14_owner_command_is_not_an_owner_observation(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    command = _owner_observation("owner-command", stamp)
    command.owner_authenticated = True
    assert command.event_origin in {
        OWNER_MANUAL_OUTBOUND_OBSERVED,
        ROUTER_AUTOMATED_OUTBOUND_OBSERVED,
        UNKNOWN_FROM_ME,
    }
    # Internal ingress rejects this impossible mixed provenance; the transport
    # keeps authenticated self-chat on its existing OWNER_COMMAND branch.
    from attention_router.adapters.internal_ingress import InternalInboundPayload

    with pytest.raises(ValueError, match="must be from_me and not an Owner Command"):
        InternalInboundPayload.model_validate({
            "source": "wwebjs",
            "external_event_id": "owner-command",
            "event_type": "message",
            "external_actor_id": "owner@c.us",
            "content": "status",
            "event_origin": OWNER_MANUAL_OUTBOUND_OBSERVED,
            "owner_authenticated": True,
            "metadata": {
                "from_me": True,
                "from_me_classification": OWNER_MANUAL_OUTBOUND_OBSERVED,
            },
        })


def test_required_15_from_me_observation_creates_no_interaction_or_loop(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    before = canary.scalar(select(func.count()).select_from(InteractionRow))
    result = services.receive_normalized_inbound_event(
        canary,
        _owner_observation("manual-without-window", stamp),
    )
    assert result["interaction_id"] is None
    assert canary.scalar(select(func.count()).select_from(InteractionRow)) == before
    assert canary.scalar(select(func.count()).select_from(QueueRow)) == 0


def test_required_16_non_grace_policy_preserves_immediate_decision_enqueue(session):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    event = NormalizedInboundEvent(
        source="test",
        external_event_id="ordinary-1",
        event_type="message",
        received_at=stamp,
        actor_id="ordinary",
        actor_display_name="Ordinary",
        actor_category="unknown",
        channel="test",
        content="Ordinary message",
        metadata={"conversation_state": "READY", "conversation_key": "test:ordinary"},
    )
    result = services.receive_normalized_inbound_event(session, event)
    assert session.get(QueueRow, "decision:" + result["inbound_event_id"]) is not None
    assert session.scalar(select(func.count()).select_from(ConversationResponseGraceWindowRow)) == 0


def test_required_17_no_active_outbox_is_created_by_grace_release(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    _receive_canary(canary, _inbound("m1", stamp))
    process_due_grace_windows(canary, "worker", timestamp=stamp + timedelta(seconds=30))
    assert canary.scalar(select(func.count()).select_from(OutboxMessageRow)) == 0


def test_required_20_ambiguous_identity_fails_closed(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    result = _receive_canary(
        canary,
        _inbound("m-ambiguous", stamp, conversation_key="", conversation_state="AMBIGUOUS"),
    )
    assert canary.get(InteractionRow, result["id"]).state == InteractionState.WAITING.value
    assert canary.scalar(select(func.count()).select_from(ConversationResponseGraceWindowRow)) == 0
    assert canary.scalar(select(func.count()).select_from(QueueRow)) == 0


def test_released_grace_cancels_ready_intent_and_pending_outbox(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    _receive_canary(canary, _inbound("m1", stamp))
    process_due_grace_windows(canary, "worker", timestamp=stamp + timedelta(seconds=30))
    intent, outbox = _add_reversible_downstream(canary)
    services.receive_normalized_inbound_event(
        canary, _owner_observation("manual-pending-outbox", stamp + timedelta(seconds=31))
    )
    assert _window(canary).state == "CANCELED"
    assert intent.status == "CANCELLED"
    assert outbox.status == "CANCELED"


def test_processing_outbox_is_the_irreversible_human_cancel_boundary(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    _receive_canary(canary, _inbound("m1", stamp))
    process_due_grace_windows(canary, "worker", timestamp=stamp + timedelta(seconds=30))
    intent, outbox = _add_reversible_downstream(canary, outbox_status="PROCESSING")
    services.receive_normalized_inbound_event(
        canary, _owner_observation("manual-too-late", stamp + timedelta(seconds=31))
    )
    assert _window(canary).state == "RELEASED"
    assert intent.status == "QUEUED"
    assert outbox.status == "PROCESSING"


def test_delayed_old_owner_observation_does_not_cancel_newer_inbound(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    _receive_canary(canary, _inbound("m1", stamp))
    services.receive_normalized_inbound_event(
        canary, _owner_observation("old-manual", stamp - timedelta(seconds=1))
    )
    assert _window(canary).state == "OPEN"


# Shared by the deterministic PostgreSQL interleaving tests as well.
def _lineage(session, label, stamp, *, state="RELEASED", conversation_key="wwebjs:5500000000020@lid"):
    from attention_router.application.owner_reply_grace import release_grace_window

    result = _receive_canary(session, _inbound(label, stamp, conversation_key=conversation_key))
    window = session.scalar(select(ConversationResponseGraceWindowRow).where(
        ConversationResponseGraceWindowRow.anchor_interaction_id == result["id"]
    ))
    if state != "OPEN":
        assert release_grace_window(
            session, window, expected_generation=window.generation,
            worker="test", timestamp=stamp + timedelta(seconds=30),
        )
        window.state = state
    session.flush()
    return window


def _lineage_snapshot(session, window, *, include_audits=True):
    """Compare all persisted fields, not just historical window status."""
    from copy import deepcopy

    members = session.scalars(select(ConversationResponseGraceInboundRow).where(
        ConversationResponseGraceInboundRow.grace_window_id == window.id
    )).all()
    interaction_ids = [member.interaction_id for member in members]
    decisions = session.scalars(select(AgentDecisionRow).where(
        AgentDecisionRow.interaction_id.in_(interaction_ids)
    )).all()
    rows = [window, *members, *decisions]
    rows += session.scalars(select(InboundEventRow).where(
        InboundEventRow.id.in_([member.inbound_event_id for member in members])
    )).all()
    if include_audits:
        rows += session.scalars(select(AuditEventRow).where(
            AuditEventRow.interaction_id.in_(interaction_ids)
        )).all()
    rows += session.scalars(select(InteractionRow).where(InteractionRow.id.in_(interaction_ids))).all()
    rows += session.scalars(select(AgentExecutionIntentRow).where(
        AgentExecutionIntentRow.agent_decision_id.in_([decision.id for decision in decisions])
    )).all()
    rows += session.scalars(select(OutboxMessageRow).where(
        OutboxMessageRow.interaction_id.in_(interaction_ids)
    )).all()
    rows += session.scalars(select(QueueRow).where(
        QueueRow.payload["grace_window_id"].as_string() == window.id
    )).all()
    return deepcopy({(row.__tablename__, row.id): {
        column.name: (
            value.replace(tzinfo=timezone.utc) if isinstance(value, datetime) and value.tzinfo is None else value
        ) for column in row.__table__.columns for value in [getattr(row, column.name)]
    } for row in rows})


def _observation_audits(session, receipt_id):
    return session.scalars(select(AuditEventRow).where(
        AuditEventRow.causation_id == receipt_id
    )).all()


@pytest.mark.parametrize("current_state", ["OPEN", "RELEASED", "CANCELED", "SUPERSEDED", "IRREVERSIBLE"])
def test_current_lineage_is_barrier_and_preserves_all_history(canary, current_state):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    old = [_lineage(canary, f"history-{n}", stamp - timedelta(minutes=n)) for n in [29, 26]]
    for window in old:
        _add_reversible_downstream(canary, window=window, blocked_without_outbox=True)
    before = [_lineage_snapshot(canary, window) for window in old]
    current = _lineage(canary, "current", stamp,
                       state="RELEASED" if current_state == "IRREVERSIBLE" else current_state)
    if current_state == "IRREVERSIBLE":
        _add_reversible_downstream(canary, window=current, outbox_status="PROCESSING")
    elif current_state == "RELEASED":
        _add_reversible_downstream(canary, window=current, blocked_without_outbox=True)
    current_before = _lineage_snapshot(canary, current, include_audits=False)
    observation = _owner_observation("takeover", stamp + timedelta(seconds=31))
    receipt = services.receive_normalized_inbound_event(canary, observation)
    canary.flush()
    if current_state in {"OPEN", "RELEASED"}:
        assert current.state == "CANCELED"
    else:
        assert _lineage_snapshot(canary, current, include_audits=False) == current_before
    assert [_lineage_snapshot(canary, window) for window in old] == before
    audits = _observation_audits(canary, receipt["receipt_id"])
    assert sum(row.event_type == "grace.canceled" for row in audits) == int(current_state in {"OPEN", "RELEASED"})
    if current_state == "IRREVERSIBLE":
        assert sum(row.event_type == "human_cancel.too_late" for row in audits) == 1
    canary.commit()
    assert services.receive_normalized_inbound_event(canary, observation) == receipt
    assert len(_observation_audits(canary, receipt["receipt_id"])) == len(audits)


def test_stale_observation_never_falls_back_to_released_history(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    old = _lineage(canary, "old", stamp - timedelta(minutes=29))
    _add_reversible_downstream(canary, window=old, blocked_without_outbox=True)
    current = _lineage(canary, "current", stamp, state="OPEN")
    before = [_lineage_snapshot(canary, window) for window in [old, current]]
    observation = _owner_observation("stale", stamp - timedelta(seconds=1))
    # A delayed receipt must still use the owner's occurrence time.
    observation = observation.model_copy(update={"received_at": stamp + timedelta(seconds=60)})
    result = services.receive_normalized_inbound_event(canary, observation)
    assert [_lineage_snapshot(canary, window) for window in [old, current]] == before
    types = [row.event_type for row in _observation_audits(canary, result["receipt_id"])]
    assert "human_cancel.stale_observation" in types
    assert "grace.canceled" not in types


def test_same_occurred_owner_received_after_cancels_current_open_once(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    inbound = _with_timestamps(
        _inbound("same-occurred-owner-after-inbound", stamp),
        occurred_at=stamp,
        received_at=stamp + timedelta(milliseconds=600),
    )
    _receive_canary(canary, inbound)
    observation = _with_timestamps(
        _owner_observation("same-occurred-owner-after", stamp),
        occurred_at=stamp,
        received_at=stamp + timedelta(milliseconds=900),
    )

    receipt = services.receive_normalized_inbound_event(canary, observation)
    window = _window(canary)
    canceled_at = window.canceled_at
    audits = _observation_audits(canary, receipt["receipt_id"])

    assert window.state == "CANCELED"
    assert sum(row.event_type == "grace.canceled" for row in audits) == 1
    canary.commit()
    assert services.receive_normalized_inbound_event(canary, observation) == receipt
    assert window.canceled_at == canceled_at
    assert len(_observation_audits(canary, receipt["receipt_id"])) == len(audits)


def test_same_occurred_owner_received_before_is_stale(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    inbound = _with_timestamps(
        _inbound("same-occurred-owner-before-inbound", stamp),
        occurred_at=stamp,
        received_at=stamp + timedelta(milliseconds=600),
    )
    _receive_canary(canary, inbound)
    observation = _with_timestamps(
        _owner_observation("same-occurred-owner-before", stamp),
        occurred_at=stamp,
        received_at=stamp + timedelta(milliseconds=300),
    )

    receipt = services.receive_normalized_inbound_event(canary, observation)
    types = [row.event_type for row in _observation_audits(canary, receipt["receipt_id"])]

    assert _window(canary).state == "OPEN"
    assert "human_cancel.stale_observation" in types
    assert "grace.canceled" not in types


def test_owner_occurred_before_is_stale_even_when_received_after(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    inbound = _with_timestamps(
        _inbound("owner-occurred-before-inbound", stamp),
        occurred_at=stamp,
        received_at=stamp + timedelta(milliseconds=600),
    )
    _receive_canary(canary, inbound)
    observation = _with_timestamps(
        _owner_observation("owner-occurred-before", stamp),
        occurred_at=stamp - timedelta(seconds=1),
        received_at=stamp + timedelta(seconds=60),
    )

    receipt = services.receive_normalized_inbound_event(canary, observation)
    types = [row.event_type for row in _observation_audits(canary, receipt["receipt_id"])]

    assert _window(canary).state == "OPEN"
    assert "human_cancel.stale_observation" in types
    assert "grace.canceled" not in types


def test_owner_occurred_after_takes_over_even_when_received_before(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    inbound = _with_timestamps(
        _inbound("owner-occurred-after-inbound", stamp),
        occurred_at=stamp,
        received_at=stamp + timedelta(milliseconds=600),
    )
    _receive_canary(canary, inbound)
    observation = _with_timestamps(
        _owner_observation("owner-occurred-after", stamp),
        occurred_at=stamp + timedelta(seconds=1),
        received_at=stamp + timedelta(milliseconds=300),
    )

    services.receive_normalized_inbound_event(canary, observation)

    assert _window(canary).state == "CANCELED"


def test_missing_occurred_at_falls_back_to_received_order(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    inbound = _with_timestamps(
        _inbound("missing-occurred-inbound", stamp),
        occurred_at=None,
        received_at=stamp + timedelta(milliseconds=600),
    )
    _receive_canary(canary, inbound)
    observation = _with_timestamps(
        _owner_observation("missing-occurred-owner", stamp),
        occurred_at=None,
        received_at=stamp + timedelta(milliseconds=900),
    )

    services.receive_normalized_inbound_event(canary, observation)

    assert _window(canary).state == "CANCELED"


def test_lineage_reference_uses_max_causal_membership_not_receipt_anchor(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    first = _with_timestamps(
        _inbound("causal-max-first", stamp),
        occurred_at=stamp + timedelta(seconds=20),
        received_at=stamp + timedelta(milliseconds=100),
    )
    second = _with_timestamps(
        _inbound("receipt-anchor-second", stamp),
        occurred_at=stamp + timedelta(seconds=5),
        received_at=stamp + timedelta(seconds=10),
    )
    _receive_canary(canary, first)
    _receive_canary(canary, second)
    window = _window(canary)
    anchor = canary.get(InboundEventRow, window.anchor_event_id)
    observation = _with_timestamps(
        _owner_observation("between-anchor-and-causal-max", stamp),
        occurred_at=stamp + timedelta(seconds=10),
        received_at=stamp + timedelta(seconds=30),
    )

    receipt = services.receive_normalized_inbound_event(canary, observation)
    types = [row.event_type for row in _observation_audits(canary, receipt["receipt_id"])]

    assert anchor.external_event_id == "receipt-anchor-second"
    assert window.state == "OPEN"
    assert "human_cancel.stale_observation" in types


def test_out_of_order_membership_keeps_operational_anchor_but_advances_causal_max(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    first = _with_timestamps(
        _inbound("operational-anchor", stamp),
        occurred_at=stamp + timedelta(seconds=20),
        received_at=stamp + timedelta(seconds=20),
    )
    _receive_canary(canary, first)
    window = _window(canary)
    before = (
        window.last_inbound_at,
        window.due_at,
        window.anchor_event_id,
        window.anchor_interaction_id,
    )
    late = _with_timestamps(
        _inbound("late-higher-causal-key", stamp),
        occurred_at=stamp + timedelta(seconds=30),
        received_at=stamp + timedelta(seconds=5),
    )
    _receive_canary(canary, late)
    observation = _with_timestamps(
        _owner_observation("stale-against-membership-max", stamp),
        occurred_at=stamp + timedelta(seconds=25),
        received_at=stamp + timedelta(seconds=40),
    )

    receipt = services.receive_normalized_inbound_event(canary, observation)
    types = [row.event_type for row in _observation_audits(canary, receipt["receipt_id"])]

    assert (
        window.last_inbound_at,
        window.due_at,
        window.anchor_event_id,
        window.anchor_interaction_id,
    ) == before
    assert window.generation == 2
    assert window.state == "OPEN"
    assert "human_cancel.stale_observation" in types


@pytest.mark.parametrize(
    ("owner_received_ms", "inbound_received_ms", "preempted"),
    [(900, 600, True), (300, 600, False)],
)
def test_prior_owner_same_occurred_uses_received_at_tie_breaker(
    canary, owner_received_ms, inbound_received_ms, preempted
):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    observation = _with_timestamps(
        _owner_observation(f"prior-owner-{preempted}", stamp),
        occurred_at=stamp,
        received_at=stamp + timedelta(milliseconds=owner_received_ms),
    )
    services.receive_normalized_inbound_event(canary, observation)
    inbound = _with_timestamps(
        _inbound(f"late-inbound-{preempted}", stamp),
        occurred_at=stamp,
        received_at=stamp + timedelta(milliseconds=inbound_received_ms),
    )

    result = _receive_canary(canary, inbound)
    interaction = canary.get(InteractionRow, result["id"])
    receipt = canary.scalar(
        select(InboundEventRow).where(
            InboundEventRow.external_event_id == inbound.external_event_id
        )
    )
    audits_before = canary.scalars(
        select(AuditEventRow).where(AuditEventRow.causation_id == receipt.id)
    ).all()
    preemptions_before = sum(
        row.event_type == "grace.inbound_preempted_by_owner_reply"
        for row in audits_before
    )

    if preempted:
        assert interaction.state == "CANCELED_BY_HUMAN_REPLY"
        assert _window(canary) is None
        assert any(
            row.event_type == "grace.inbound_preempted_by_owner_reply"
            for row in audits_before
        )
    else:
        assert interaction.state == "WAITING"
        assert _window(canary).state == "OPEN"
        assert all(
            row.event_type != "grace.inbound_preempted_by_owner_reply"
            for row in audits_before
        )
    window_before = _window(canary)
    window_snapshot = (
        None
        if window_before is None
        else (window_before.id, window_before.state, window_before.generation)
    )
    canary.commit()
    replay = _receive_canary(canary, inbound)
    audits_after = canary.scalars(
        select(AuditEventRow).where(AuditEventRow.causation_id == receipt.id)
    ).all()
    window_after = _window(canary)

    assert replay["id"] == result["id"]
    assert sum(
        row.event_type == "grace.inbound_preempted_by_owner_reply"
        for row in audits_after
    ) == preemptions_before
    assert (
        None
        if window_after is None
        else (window_after.id, window_after.state, window_after.generation)
    ) == window_snapshot


def test_late_inbound_is_preempted_without_fake_window_cancel(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    services.receive_normalized_inbound_event(canary, _owner_observation("owner-first", stamp))
    result = _receive_canary(canary, _inbound("late", stamp - timedelta(seconds=1)))
    assert canary.get(InteractionRow, result["id"]).state == "CANCELED_BY_HUMAN_REPLY"
    assert canary.scalar(select(func.count()).select_from(ConversationResponseGraceWindowRow)) == 0
    types = canary.scalars(select(AuditEventRow.event_type)).all()
    assert "grace.inbound_preempted_by_owner_reply" in types
    assert "grace.canceled" not in types


def test_out_of_order_extension_preserves_latest_anchor_and_deadline(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    window = _lineage(canary, "first", stamp, state="OPEN")
    _receive_canary(canary, _inbound("latest", stamp + timedelta(seconds=20)))
    before = (window.last_inbound_at, window.due_at, window.anchor_event_id, window.anchor_interaction_id)
    late = _inbound("late", stamp + timedelta(seconds=5))
    _receive_canary(canary, late)
    assert (window.last_inbound_at, window.due_at, window.anchor_event_id, window.anchor_interaction_id) == before
    assert window.generation == 3
    assert canary.scalar(select(func.count()).select_from(ConversationResponseGraceInboundRow)) == 3
    canary.commit()
    _receive_canary(canary, late)
    assert window.generation == 3
    assert process_due_grace_windows(canary, "test", timestamp=stamp + timedelta(seconds=35)) == 0
    assert process_due_grace_windows(canary, "test", timestamp=stamp + timedelta(seconds=50)) == 1
    assert canary.scalar(select(QueueRow)).payload["event_id"] == window.anchor_event_id


@pytest.mark.parametrize("tie", [True, False])
def test_temporal_tie_fails_closed_and_created_at_breaks_only_partial_tie(canary, tie):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    old = _lineage(canary, "old", stamp - timedelta(minutes=29))
    first = _lineage(canary, "first", stamp - timedelta(minutes=1))
    second = _lineage(canary, "second", stamp)
    first.last_inbound_at = second.last_inbound_at
    first.due_at = second.due_at
    if tie:
        first.created_at = second.created_at
    canary.flush()
    before = [_lineage_snapshot(canary, window) for window in [old, first, second]]
    receipt = services.receive_normalized_inbound_event(canary, _owner_observation("owner", stamp + timedelta(seconds=31)))
    types = [row.event_type for row in _observation_audits(canary, receipt["receipt_id"])]
    if tie:
        assert [_lineage_snapshot(canary, window) for window in [old, first, second]] == before
        assert "human_cancel.ambiguous_lineage" in types
        assert "grace.canceled" not in types
    else:
        assert second.state == "CANCELED"
        assert [_lineage_snapshot(canary, window) for window in [old, first]] == before[:2]



def test_last_inbound_precedes_created_at_in_barrier_order(canary):
    stamp = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    first = _lineage(canary, "earlier-created", stamp - timedelta(minutes=1))
    second = _lineage(canary, "later-created", stamp)
    first.last_inbound_at = stamp + timedelta(seconds=1)
    first.due_at = stamp + timedelta(seconds=31)
    canary.flush()
    before = _lineage_snapshot(canary, second)
    services.receive_normalized_inbound_event(canary, _owner_observation("owner", stamp + timedelta(seconds=32)))
    assert first.state == "CANCELED"
    assert _lineage_snapshot(canary, second) == before
