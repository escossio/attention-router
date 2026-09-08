from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
import subprocess
import sys
import threading
import uuid

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from attention_router.application import services
from attention_router.application.owner_automation_control import (
    automation_denial_reason,
    set_automatic_responses_enabled,
)
from attention_router.config import settings
from attention_router.core.events import OperatorAuthority, OwnerCommandUnauthorized
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    AgentDecisionRow,
    AgentExecutionIntentRow,
    AuditEventRow,
    AutonomyEvaluationRow,
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
    OwnerAutomationControlChangeRow,
    OwnerAutomationControlRow,
)
from attention_router.infrastructure.repository import seed_policies
from tests.integration.test_postgres_owner_control import _event, _install


pytestmark = pytest.mark.postgres


@pytest.fixture
def Session(pg_url):
    """Per-test database: committed concurrency fixtures cannot leak into dispatch."""
    name = f"owner_automation_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(pg_url.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = pg_url.rsplit("/", 1)[0] + "/" + name
    engine = create_engine(url)
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env={**os.environ, "DATABASE_URL": url},
            check=True,
            capture_output=True,
        )
        factory = sessionmaker(engine, expire_on_commit=False)
        with factory() as session:
            seed_policies(session)
            session.commit()
        yield factory
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def _control(session, context, enabled, event_id):
    return set_automatic_responses_enabled(
        session,
        tenant_id=context["tenant_id"],
        represented_owner_actor_key=context["owner_actor_id"],
        enabled=enabled,
        authority=OperatorAuthority(
            tenant_id=context["tenant_id"],
            operator_actor_id=context["owner_actor_id"],
            authenticated=True,
            roles=["OWNER"],
        ),
        source_channel="wwebjs-owner-control",
        source_event_id=event_id,
        provenance={"authentication_mechanism": "WWEBJS_AUTHENTICATED_SELF_CHAT"},
    )


def test_global_command_concurrent_replay_is_one_change_and_confirmation(Session):
    context = _install(Session, uuid.uuid4().hex[:8])
    barrier = threading.Barrier(2)

    def receive():
        with Session() as session:
            barrier.wait(timeout=10)
            result = services.receive_normalized_inbound_event(
                session, _event(context, "global-pause-" + "x" * 160, "pausa")
            )
            session.commit()
            return result["id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: receive(), range(2)))
    assert results[0] == results[1]
    with Session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(OwnerAutomationControlChangeRow)
                .where(OwnerAutomationControlChangeRow.tenant_id == context["tenant_id"])
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxMessageRow)
                .where(OutboxMessageRow.interaction_id == results[0])
            )
            == 1
        )
        assert automation_denial_reason(session, context["tenant_id"]) == "OWNER_AUTOMATION_PAUSED"
        logged = session.scalar(
            select(AuditEventRow).where(
                AuditEventRow.tenant_id == context["tenant_id"],
                AuditEventRow.event_type == "owner_automation_control.changed",
            )
        )
        assert len(logged.causation_id) == 36  # Receipt UUID, not the 172-character provider ID.
        assert "source_event_id" not in logged.payload
        assert len(logged.payload["source_event_hash"]) == 64


@pytest.mark.parametrize("same_event", [False, True])
def test_global_concurrent_pause_serializes_revision_and_event_idempotency(Session, same_event):
    context = _install(Session, uuid.uuid4().hex[:8])
    barrier = threading.Barrier(2)

    def pause(index):
        with Session() as session:
            barrier.wait(timeout=10)
            result = _control(
                session, context, False, "same-pause" if same_event else f"pause-{index}"
            )
            session.commit()
            return result.changed

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(pause, range(2))) == [False, True]
    with Session() as session:
        row = session.scalar(
            select(OwnerAutomationControlRow).where(
                OwnerAutomationControlRow.tenant_id == context["tenant_id"]
            )
        )
        assert row.revision == 1
        assert session.scalar(
            select(func.count())
            .select_from(OwnerAutomationControlChangeRow)
            .where(OwnerAutomationControlChangeRow.control_id == row.id)
        ) == (1 if same_event else 2)
        assert _control(session, context, True, "resume").control.revision == 2
        session.commit()
    with Session() as session:
        assert automation_denial_reason(session, context["tenant_id"]) is None


def test_tenant_isolation_and_owner_authority(Session):
    first = _install(Session, uuid.uuid4().hex[:8])
    second = _install(Session, uuid.uuid4().hex[:8])
    with Session() as session:
        _control(session, first, False, "same-external-id")
        _control(session, second, True, "same-external-id")
        session.commit()
    with Session() as session:
        assert automation_denial_reason(session, first["tenant_id"]) == "OWNER_AUTOMATION_PAUSED"
        assert automation_denial_reason(session, second["tenant_id"]) is None
        with pytest.raises(OwnerCommandUnauthorized):
            _control(session, {**first, "owner_actor_id": second["owner_actor_id"]}, False, "wrong")


def _pending_automatic(Session, context):
    stamp = now_utc()
    with Session() as s:
        interaction = InteractionRow(
            id=new_id(),
            tenant_id=context["tenant_id"],
            event_type="message",
            contact_id="peer-fixture",
            contact_name="Peer",
            relationship_category="test",
            inbound_text="test",
            state="active",
            correlation_id=new_id(),
            created_at=stamp,
            updated_at=stamp,
        )
        s.add(interaction)
        s.flush()
        event = InboundEventRow(
            id=new_id(),
            tenant_id=context["tenant_id"],
            source="wwebjs",
            external_event_id=new_id(),
            event_type="message",
            received_at=stamp,
            payload={"external_actor_id": "5500000000024@c.us", "channel": "whatsapp",
                "event_type":"message", "event_origin":"EXTERNAL_INBOUND", "owner_authenticated":False,
                "metadata":{"from_me":False,"source_account":"default","conversation_state":"READY",
                    "conversation_key":"wwebjs:5500000000024@c.us"}},
            lineage_classification="ORGANIC",
            payload_hash=new_id(),
            status="processed",
            interaction_id=interaction.id,
            correlation_id=interaction.correlation_id,
        )
        s.add(event)
        s.flush()
        decision = AgentDecisionRow(
            id=new_id(),
            event_id=event.id,
            interaction_id=interaction.id,
            decision_pipeline_version="v1",
            decision_type="RESPOND",
            recommended_action="respond",
            proposed_response="test",
            escalation_required=False,
            confidence=1,
            missing_information=[],
            execution_allowed=False,
            external_delivery_allowed=False,
            reasoning_summary="test",
            status="DRY_RUN",
            created_at=stamp,
        )
        s.add(decision)
        s.flush()
        evaluation = AutonomyEvaluationRow(
            id=new_id(),
            agent_decision_id=decision.id,
            event_id=event.id,
            interaction_id=interaction.id,
            blueprint_mode="AUTO_ALLOWED",
            policy_mode="AUTO_ALLOWED",
            effective_mode="AUTO_ALLOWED",
            decision_type="RESPOND",
            recommended_action="respond",
            action_allowed=True,
            actor_scope_valid=True,
            audience_scope_valid=True,
            freshness_valid=True,
            from_me=False,
            automatic_execution_allowed=True,
            reason_code="AUTO_ALLOWED",
            created_at=stamp,
        )
        s.add(evaluation)
        s.flush()
        intent = AgentExecutionIntentRow(
            id=new_id(),
            agent_decision_id=decision.id,
            autonomy_evaluation_id=evaluation.id,
            authorization_source="POLICY_AUTONOMY",
            intent_type="WHATSAPP_RESPONSE",
            effective_response_snapshot="test",
            status="READY",
            release_status="RELEASED",
            execution_allowed=True,
            external_delivery_allowed=True,
            idempotency_key=new_id(),
            created_at=stamp,
        )
        s.add(intent)
        s.flush()
        outbox = OutboxMessageRow(
            id=new_id(),
            interaction_id=interaction.id,
            action_type="agent_execution_text",
            destination="local_transport",
            payload={"text": "test", "external_actor_id": "5500000000024@c.us"},
            status="PENDING",
            created_at=stamp,
            available_at=stamp,
            idempotency_key=new_id(),
            execution_intent_id=intent.id,
        )
        s.add(outbox)
        s.commit()
        return outbox.id


def test_pause_committed_after_claim_blocks_send_with_stale_session_cache(Session, monkeypatch):
    context = _install(Session, uuid.uuid4().hex[:8])
    with Session() as s:
        _control(s, context, True, "initial")
        s.commit()
    outbox_id = _pending_automatic(Session, context)
    original = services.claim_outbox

    def claim_then_pause(session, worker, limit):
        # Prime the identity map with enabled=True before a different session pauses.
        cached = session.scalar(
            select(OwnerAutomationControlRow).where(
                OwnerAutomationControlRow.tenant_id == context["tenant_id"]
            )
        )
        assert cached.automatic_responses_enabled
        rows = original(session, worker, limit)
        with Session() as control_session:
            _control(control_session, context, False, "pause")
            control_session.commit()
        assert cached.automatic_responses_enabled  # Intentionally stale cached ORM row.
        return rows

    monkeypatch.setattr(services, "claim_outbox", claim_then_pause)
    monkeypatch.setattr(
        services.local_transport_outbound,
        "dispatch_outbox",
        lambda _: pytest.fail("transport must never be called"),
    )
    with Session() as s:
        services.process_outbox(s)
    with Session() as s:
        row = s.get(OutboxMessageRow, outbox_id)
        assert row.status == "PENDING"
        assert row.last_error == "OWNER_AUTOMATION_PAUSED"
        assert row.attempt_count == 0


def test_inflight_send_serializes_pause_until_effect_finishes(Session, monkeypatch):
    context = _install(Session, uuid.uuid4().hex[:8])
    outbox_id = _pending_automatic(Session, context)
    for name in (
        "agent_execution_enabled",
        "external_delivery_enabled",
        "autonomous_execution_enabled",
    ):
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr(
        settings,
        "autonomous_execution_activated_at",
        (now_utc() - timedelta(seconds=5)).isoformat(),
    )
    sending, finish, pause_attempted = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def send(row):
        assert row.id == outbox_id
        calls.append(row.id)
        sending.set()
        assert finish.wait(timeout=10)
        return type("Result", (), {"status": "sent", "response": {}})()

    monkeypatch.setattr(services.local_transport_outbound, "dispatch_outbox", send)

    def dispatch():
        with Session() as s:
            services.process_outbox(s)

    def pause():
        with Session() as s:
            # A DB lock timeout proves actual contention, without timing sleeps.
            s.execute(text("SET LOCAL lock_timeout = '200ms'"))
            from sqlalchemy.exc import OperationalError

            with pytest.raises(OperationalError):
                _control(s, context, False, "pause-blocked")
            s.rollback()
            pause_attempted.set()
            assert finish.wait(timeout=10)
            _control(s, context, False, "pause-after-send")
            s.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        dispatch_future = pool.submit(dispatch)
        assert sending.wait(timeout=10)
        pause_future = pool.submit(pause)
        try:
            assert pause_attempted.wait(timeout=10)
        finally:
            finish.set()
        dispatch_future.result(timeout=10)
        pause_future.result(timeout=10)
    assert calls == [outbox_id]
    with Session() as s:
        assert automation_denial_reason(s, context["tenant_id"]) == "OWNER_AUTOMATION_PAUSED"


def test_migration_upgrade_downgrade_upgrade(pg_url):
    # Dedicated database, never downgrade the other tests' shared database.
    name = f"owner_automation_migration_{uuid.uuid4().hex[:8]}"
    admin_url = pg_url.rsplit("/", 1)[0] + "/postgres"
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = pg_url.rsplit("/", 1)[0] + "/" + name
    env = {**os.environ, "DATABASE_URL": url}
    try:
        for target, verb in [
            ("head", "upgrade"),
            ("0032_owner_operational_controls", "downgrade"),
            ("head", "upgrade"),
        ]:
            subprocess.run(
                [sys.executable, "-m", "alembic", verb, target],
                env=env,
                check=True,
                capture_output=True,
            )
            test_engine = create_engine(url)
            with test_engine.connect() as connection:
                exists = connection.scalar(text("SELECT to_regclass('owner_automation_controls')"))
                assert bool(exists) == (verb == "upgrade")
                if verb == "upgrade":
                    assert (
                        connection.scalar(text("SELECT count(*) FROM owner_automation_controls"))
                        == 0
                    )
            test_engine.dispose()
    finally:
        with engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        engine.dispose()
