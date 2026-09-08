from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from attention_router.adapters.inbound import NormalizedInboundEvent
from attention_router.application import services
from attention_router.application.owner_operational_control import (
    set_owner_reply_grace_seconds,
)
from attention_router.application.owner_reply_grace import (
    CANARY_ACTOR_ID,
    CANARY_AUDIENCE,
    CANARY_BINDING_ID,
    CANARY_POLICY_ID,
    OWNER_MANUAL_OUTBOUND_OBSERVED,
    process_due_grace_windows,
    release_grace_window,
)
from attention_router.application.platform.entities import create_relationship
from attention_router.core.entities import EntityReference
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import now_utc
from attention_router.infrastructure.models import (
    ActorBindingRow,
    AuditEventRow,
    OutboxMessageRow,
    ConversationResponseGraceWindowRow,
    PolicyRow,
    QueueRow,
)
from attention_router.infrastructure.repository import (
    ensure_policy_version,
    provision_morgan_owner_reply_grace_policy,
)


pytestmark = pytest.mark.postgres

PEER = "5500000000023@c.us"
RACE_CONVERSATION = "wwebjs:99000010@lid"
TRIPLE_CONVERSATION = "wwebjs:99000011@lid"
CONTROL_M2_CONVERSATION = "wwebjs:99000012@lid"
CONTROL_TIMEOUT_CONVERSATION = "wwebjs:99000013@lid"
CONTROL_MANUAL_CONVERSATION = "wwebjs:99000014@lid"


def _install_canary(session) -> None:
    if session.get(ActorBindingRow, CANARY_BINDING_ID) is not None:
        return
    stamp = now_utc()
    session.add_all([
        ActorBindingRow(
            id="postgres-owner-binding", tenant_id=DEFAULT_TENANT_ID, source="wwebjs",
            external_actor_id="postgres-owner@c.us", actor_key="postgres-owner",
            display_name="Alex", actor_category="owner", active_context=None,
            is_active=True, binding_metadata={"owner": True}, created_at=stamp, updated_at=stamp,
        ),
        ActorBindingRow(
            id=CANARY_BINDING_ID, tenant_id=DEFAULT_TENANT_ID, source="wwebjs",
            external_actor_id=PEER, actor_key=CANARY_ACTOR_ID,
            display_name="Sr. Morgan Example", actor_category="family_core",
            active_context=None, is_active=True,
            binding_metadata={"audience": CANARY_AUDIENCE, "canary": True},
            created_at=stamp, updated_at=stamp,
        ),
    ])
    config = {
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
            "actor_ids": [CANARY_ACTOR_ID], "binding_ids": [CANARY_BINDING_ID],
            "audiences": [CANARY_AUDIENCE],
        },
    }
    policy = PolicyRow(
        **{key: config[key] for key in PolicyRow.__table__.columns.keys() if key in config},
        tenant_id=DEFAULT_TENANT_ID,
        is_active=True,
    )
    session.add(policy)
    session.flush()
    ensure_policy_version(session, policy, config, "postgres-test")
    create_relationship(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        source=EntityReference(entity_type="ACTOR", entity_id=CANARY_ACTOR_ID),
        target=EntityReference(entity_type="ACTOR", entity_id="postgres-owner"),
        relationship_type="pai",
        metadata_sanitized={"authorization": "explicit_human_authorization"},
    )
    provision_morgan_owner_reply_grace_policy(session, origin="postgres-test")
    session.commit()


def _inbound(event_id: str, stamp: datetime, conversation_key: str) -> NormalizedInboundEvent:
    return NormalizedInboundEvent(
        source="wwebjs", external_event_id=event_id, event_type="message",
        occurred_at=stamp, received_at=stamp, actor_id=PEER,
        actor_display_name="Sr. Morgan Example", actor_category="family_core",
        channel="whatsapp", content=f"Message {event_id}",
        event_origin="EXTERNAL_INBOUND",
        metadata={
            "conversation_key": conversation_key, "conversation_state": "READY",
            "source_account": "default", "peer_identifiers": [PEER, conversation_key[7:]], "from_me": False,
        },
    )


def _manual(event_id: str, stamp: datetime, conversation_key: str) -> NormalizedInboundEvent:
    return NormalizedInboundEvent(
        source="wwebjs", external_event_id=event_id, event_type="message",
        occurred_at=stamp, received_at=stamp, actor_id=PEER,
        actor_display_name="Owner outbound observation",
        actor_category="owner_outbound_observation", channel="whatsapp",
        content="[owner outbound observation]", event_origin=OWNER_MANUAL_OUTBOUND_OBSERVED,
        metadata={
            "conversation_key": conversation_key, "conversation_state": "READY",
            "source_account": "default", "peer_identifiers": [PEER], "from_me": True,
            "from_me_classification": OWNER_MANUAL_OUTBOUND_OBSERVED,
        },
    )


def _set_seconds(session, seconds: int, command_id: str, stamp: datetime):
    return set_owner_reply_grace_seconds(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        represented_owner_actor_key="postgres-owner",
        policy_id=None,
        grace_seconds=seconds,
        updated_by_actor_key="postgres-owner",
        authorization_source="TEST",
        source_channel="postgres-control-test",
        source_event_id=command_id,
        timestamp=stamp,
    )


def test_required_18_timeout_and_owner_reply_have_one_transactional_outcome(Session):
    with Session() as setup:
        _install_canary(setup)
        due = datetime.now(timezone.utc) - timedelta(seconds=1)
        services.receive_normalized_inbound_event(
            setup, _inbound("race-m1", due - timedelta(seconds=30), RACE_CONVERSATION)
        )
        setup.commit()

    barrier = Barrier(2)

    def release():
        with Session() as session:
            barrier.wait()
            result = process_due_grace_windows(session, "release-worker", timestamp=due)
            session.commit()
            return result

    def cancel():
        with Session() as session:
            barrier.wait()
            services.receive_normalized_inbound_event(
                session, _manual("race-owner", due, RACE_CONVERSATION)
            )
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(release), pool.submit(cancel)]
        for future in futures:
            future.result(timeout=10)

    with Session() as verify:
        window = verify.scalar(
            select(ConversationResponseGraceWindowRow).where(
                ConversationResponseGraceWindowRow.conversation_key == RACE_CONVERSATION
            )
        )
        queues = verify.scalars(
            select(QueueRow).where(
                QueueRow.payload["grace_window_id"].as_string() == window.id
            )
        ).all()
        assert window.state == "CANCELED"
        assert len(queues) <= 1
        assert all(queue.status == "CANCELED" for queue in queues)


def test_required_19_timeout_m2_and_owner_reply_never_double_release(Session):
    with Session() as setup:
        _install_canary(setup)
        due = datetime.now(timezone.utc) - timedelta(seconds=1)
        services.receive_normalized_inbound_event(
            setup, _inbound("triple-m1", due - timedelta(seconds=30), TRIPLE_CONVERSATION)
        )
        setup.commit()

    barrier = Barrier(3)

    def release():
        with Session() as session:
            barrier.wait()
            process_due_grace_windows(session, "release-worker", timestamp=due)
            session.commit()

    def extend():
        with Session() as session:
            barrier.wait()
            services.receive_normalized_inbound_event(
                session,
                _inbound(
                    "triple-m2", due + timedelta(milliseconds=1), TRIPLE_CONVERSATION
                ),
            )
            session.commit()

    def cancel():
        with Session() as session:
            barrier.wait()
            services.receive_normalized_inbound_event(
                session,
                _manual(
                    "triple-owner", due + timedelta(milliseconds=2), TRIPLE_CONVERSATION
                ),
            )
            session.commit()

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(release), pool.submit(extend), pool.submit(cancel)]
        for future in futures:
            future.result(timeout=10)

    with Session() as verify:
        windows = verify.scalars(
            select(ConversationResponseGraceWindowRow).where(
                ConversationResponseGraceWindowRow.conversation_key == TRIPLE_CONVERSATION
            )
        ).all()
        window_ids = [window.id for window in windows]
        target_queue = select(QueueRow).where(
            QueueRow.payload["grace_window_id"].as_string().in_(window_ids)
        )
        assert len(verify.scalars(target_queue).all()) <= 1
        # A released predecessor survives if M2 opened a newer lineage before
        # takeover. Only queues belonging to canceled windows must be suppressed.
        for queue in verify.scalars(target_queue).all():
            window = next(item for item in windows if item.id == queue.payload["grace_window_id"])
            assert queue.status == ("CANCELED" if window.state == "CANCELED" else "PENDING")
        assert sum(window.state == "CANCELED" for window in windows) <= 1
        assert verify.scalar(
            select(func.count()).select_from(ConversationResponseGraceWindowRow).where(
                ConversationResponseGraceWindowRow.state == "OPEN",
                ConversationResponseGraceWindowRow.conversation_key == TRIPLE_CONVERSATION,
            )
        ) == 0



def _ordered_transactions(Session, first_action, second_action, *, expect_block=True):
    """Hold the winner uncommitted until PostgreSQL proves the loser is blocked."""
    started = Event()
    second_pid = []
    with Session() as first, ThreadPoolExecutor(max_workers=1) as pool:
        first_pid = first.scalar(text("select pg_backend_pid()"))
        first_action(first)

        def second_transaction():
            with Session() as second:
                second.execute(text("set local lock_timeout = '8s'"))
                second_pid.append(second.scalar(text("select pg_backend_pid()")))
                started.set()
                second_action(second)
                second.commit()

        future = pool.submit(second_transaction)
        try:
            assert started.wait(5)
            if not expect_block:
                # The due scanner must SKIP LOCKED, never wait for owner.
                future.result(timeout=5)
                first.commit()
                return
            deadline = monotonic() + 5
            with Session() as observer:
                while monotonic() < deadline:
                    blockers = observer.scalar(text("select pg_blocking_pids(:pid)"), {"pid": second_pid[0]})
                    if first_pid in blockers:
                        break
                    if future.done():
                        future.result()
                        pytest.fail("Expected an actual lock wait, not sequential execution")
                    sleep(0.01)
                else:
                    pytest.fail("Second transaction never waited for first transaction")
            first.commit()
        finally:
            first.rollback()
        future.result(timeout=10)


@pytest.mark.parametrize("competitor,current_state,inbound_after", [
    ("inbound", "OPEN", True), ("inbound", "OPEN", False),
    ("inbound", "RELEASED", True), ("inbound", "RELEASED", False),
    ("timeout", "OPEN", False), ("outbox", "RELEASED", False),
])
@pytest.mark.parametrize("owner_first", [True, False], ids=["owner-wins", "competitor-wins"])
def test_current_lineage_ordered_lock_races_preserve_history(
    Session, competitor, current_state, inbound_after, owner_first,
):
    from test_owner_reply_grace import _add_reversible_downstream, _lineage_snapshot

    key = f"wwebjs:{uuid4().int}@lid"
    stamp = datetime.now(timezone.utc) - timedelta(minutes=2)
    with Session() as setup:
        _install_canary(setup)
        windows = []
        for n, age in enumerate([29, 26, 0]):
            result = services.receive_normalized_inbound_event(
                setup, _inbound(f"{key}-m{n}", stamp - timedelta(minutes=age), key),
            )
            window = setup.scalar(select(ConversationResponseGraceWindowRow).where(
                ConversationResponseGraceWindowRow.anchor_interaction_id == result["id"]
            ))
            if age or current_state == "RELEASED":
                assert release_grace_window(setup, window, expected_generation=1, worker="setup",
                                            timestamp=stamp + timedelta(seconds=30))
            windows.append(window)
        for window in windows[:2]:
            _add_reversible_downstream(setup, window=window, blocked_without_outbox=True)
        current_id = windows[-1].id
        outbox_id = None
        if competitor == "outbox":
            _, outbox = _add_reversible_downstream(setup, window=windows[-1])
            outbox_id = outbox.id
        setup.commit()
        old_ids = [window.id for window in windows[:2]]
        before = [_lineage_snapshot(setup, window) for window in windows[:2]]

    def owner(session):
        services.receive_normalized_inbound_event(
            session, _manual(f"{key}-owner", stamp + timedelta(seconds=31), key),
        )

    def other(session):
        if competitor == "inbound":
            # Exercise both timestamp relationships and both lock orders,
            # with either an OPEN extension or a newly created lineage.
            services.receive_normalized_inbound_event(
                session, _inbound(f"{key}-new", stamp + timedelta(seconds=32 if inbound_after else 29), key),
            )
        elif competitor == "timeout":
            process_due_grace_windows(session, "ordered-timeout", timestamp=stamp + timedelta(seconds=30))
        else:
            # Claim only; never dispatch or invoke an outbound adapter.
            services.claim_outbox(session, "ordered-claim", limit=100)

    _ordered_transactions(
        Session, owner if owner_first else other, other if owner_first else owner,
        expect_block=not (competitor == "timeout" and owner_first),
    )
    with Session() as verify:
        old = [verify.get(ConversationResponseGraceWindowRow, window_id) for window_id in old_ids]
        assert [_lineage_snapshot(verify, window) for window in old] == before
        current = verify.get(ConversationResponseGraceWindowRow, current_id)
        windows = verify.scalars(select(ConversationResponseGraceWindowRow).where(
            ConversationResponseGraceWindowRow.conversation_key == key
        )).all()
        if competitor == "inbound":
            if owner_first or (current_state == "OPEN" and not inbound_after):
                assert current.state == "CANCELED"
            else:
                assert current.state == current_state
            assert sum(window.state == "OPEN" for window in windows) == int(inbound_after)
        elif competitor == "timeout":
            assert current.state == "CANCELED"
            queues = verify.scalars(select(QueueRow).where(
                QueueRow.payload["grace_window_id"].as_string() == current.id
            )).all()
            assert len(queues) == int(not owner_first)
            assert all(queue.status == "CANCELED" for queue in queues)
        else:
            assert current.state == ("CANCELED" if owner_first else "RELEASED")
            assert verify.get(OutboxMessageRow, outbox_id).status == ("CANCELED" if owner_first else "PROCESSING")
        canceled = verify.scalars(select(AuditEventRow).where(
            AuditEventRow.event_type == "grace.canceled",
            AuditEventRow.payload["grace_window_id"].as_string().in_([window.id for window in windows]),
        )).all()
        expected_cancel = competitor == "timeout" or owner_first or (competitor == "inbound" and not inbound_after)
        assert len(canceled) == int(expected_cancel)
        assert sum(window.state == "CANCELED" for window in windows) == int(expected_cancel)


def test_potentiometer_17_control_change_concurrent_with_m2_preserves_trailing_edge(Session):
    stamp = datetime.now(timezone.utc)
    with Session() as setup:
        _install_canary(setup)
        _set_seconds(setup, 30, "control-race-m2-initial", stamp)
        services.receive_normalized_inbound_event(
            setup, _inbound("control-race-m2-m1", stamp, CONTROL_M2_CONVERSATION)
        )
        setup.commit()

    barrier = Barrier(2)

    def change_control():
        with Session() as session:
            barrier.wait()
            _set_seconds(
                session, 60, "control-race-m2-change", stamp + timedelta(seconds=10)
            )
            session.commit()

    def receive_m2():
        with Session() as session:
            barrier.wait()
            services.receive_normalized_inbound_event(
                session,
                _inbound(
                    "control-race-m2-m2",
                    stamp + timedelta(seconds=10),
                    CONTROL_M2_CONVERSATION,
                ),
            )
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(change_control), pool.submit(receive_m2)]
        for future in futures:
            future.result(timeout=10)

    with Session() as verify:
        window = verify.scalar(
            select(ConversationResponseGraceWindowRow).where(
                ConversationResponseGraceWindowRow.conversation_key == CONTROL_M2_CONVERSATION
            )
        )
        assert window.state == "OPEN"
        assert window.generation == 2
        assert window.effective_grace_seconds == 60
        assert window.due_at == window.last_inbound_at + timedelta(seconds=60)
        assert verify.scalar(
            select(func.count()).select_from(QueueRow).where(
                QueueRow.payload["grace_window_id"].as_string() == window.id
            )
        ) == 0


def test_potentiometer_18_control_change_concurrent_with_timeout_has_one_result(Session):
    due = datetime.now(timezone.utc)
    inbound_at = due - timedelta(seconds=30)
    with Session() as setup:
        _install_canary(setup)
        _set_seconds(setup, 30, "control-race-timeout-initial", inbound_at)
        services.receive_normalized_inbound_event(
            setup,
            _inbound("control-race-timeout-m1", inbound_at, CONTROL_TIMEOUT_CONVERSATION),
        )
        setup.commit()

    barrier = Barrier(2)

    def release():
        with Session() as session:
            barrier.wait()
            process_due_grace_windows(session, "control-timeout-worker", timestamp=due)
            session.commit()

    def change_control():
        with Session() as session:
            barrier.wait()
            _set_seconds(session, 60, "control-race-timeout-change", due)
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(release), pool.submit(change_control)]
        for future in futures:
            future.result(timeout=10)

    with Session() as verify:
        window = verify.scalar(
            select(ConversationResponseGraceWindowRow).where(
                ConversationResponseGraceWindowRow.conversation_key
                == CONTROL_TIMEOUT_CONVERSATION
            )
        )
        queues = verify.scalars(
            select(QueueRow).where(
                QueueRow.payload["grace_window_id"].as_string() == window.id
            )
        ).all()
        assert window.state in {"OPEN", "RELEASED"}
        if window.state == "OPEN":
            assert window.effective_grace_seconds == 60
            assert window.due_at == window.last_inbound_at + timedelta(seconds=60)
            assert queues == []
        else:
            assert len(queues) == 1
        assert len(queues) <= 1


def test_potentiometer_19_control_change_concurrent_with_manual_reply_never_resurrects(Session):
    stamp = datetime.now(timezone.utc)
    with Session() as setup:
        _install_canary(setup)
        _set_seconds(setup, 30, "control-race-manual-initial", stamp)
        services.receive_normalized_inbound_event(
            setup,
            _inbound("control-race-manual-m1", stamp, CONTROL_MANUAL_CONVERSATION),
        )
        setup.commit()

    barrier = Barrier(2)

    def change_control():
        with Session() as session:
            barrier.wait()
            _set_seconds(
                session, 60, "control-race-manual-change", stamp + timedelta(seconds=10)
            )
            session.commit()

    def cancel():
        with Session() as session:
            barrier.wait()
            services.receive_normalized_inbound_event(
                session,
                _manual(
                    "control-race-manual-owner",
                    stamp + timedelta(seconds=10),
                    CONTROL_MANUAL_CONVERSATION,
                ),
            )
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(change_control), pool.submit(cancel)]
        for future in futures:
            future.result(timeout=10)

    with Session() as verify:
        window = verify.scalar(
            select(ConversationResponseGraceWindowRow).where(
                ConversationResponseGraceWindowRow.conversation_key == CONTROL_MANUAL_CONVERSATION
            )
        )
        assert window.state == "CANCELED"
        assert verify.scalar(
            select(func.count()).select_from(QueueRow).where(
                QueueRow.payload["grace_window_id"].as_string() == window.id,
                QueueRow.status == "PENDING",
            )
        ) == 0
