import hashlib
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.orm import sessionmaker

import attention_router.infrastructure.repository as repository

from attention_router.application.decision_pipeline import process_agent_decision
from attention_router.infrastructure.models import AuditEventRow, InboundEventRow, InteractionRow, MetaCallbackInboxRow, OutboxMessageRow, TimerRow
from attention_router.infrastructure.models import AgentDecisionRow
from attention_router.infrastructure.repository import audit, upsert_actor_binding
from attention_router.platform.transaction_locks import acquire_meta_provider_gate
from attention_router.web.ingress_app import (
    app,
    get_callback_session_factory,
    get_session,
)
from tests.meta_fixtures import APP_SECRET, body, meta_batch, meta_status, meta_text_message, signed_headers


pytestmark = pytest.mark.postgres


@pytest.fixture()
def client(Session, monkeypatch):
    monkeypatch.setattr("attention_router.web.ingress_app.settings.meta_app_secret", APP_SECRET)
    monkeypatch.setattr("attention_router.web.ingress_app.settings.meta_webhook_dispatch_enabled", True)

    def override():
        session = Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_session] = override
    app.dependency_overrides[get_callback_session_factory] = lambda: Session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def post_signed(client, payload):
    raw = body(payload)
    return client.post("/api/v1/ingress/meta/whatsapp/webhook", content=raw, headers=signed_headers(raw))


def process_persisted_inbound_decision(Session, message_id):
    with Session() as session:
        inbound = session.scalars(
            select(InboundEventRow).where(InboundEventRow.external_event_id == message_id)
        ).one()
        decision = process_agent_decision(session, inbound.id)
        assert decision is not None
        session.commit()
        return inbound.interaction_id, decision.id


def test_pg_meta_signed_webhook_e2e_unknown_actor(Session, client):
    message_id = f"wamid.pg.{uuid.uuid4()}"
    response = post_signed(client, meta_text_message(message_id, text="hello pg"))
    assert response.status_code == 200
    assert response.json()["processed"] == 1
    process_persisted_inbound_decision(Session, message_id)
    with Session() as session:
        inbound = session.scalars(select(InboundEventRow).where(InboundEventRow.external_event_id == message_id)).one()
        assert inbound.source == "meta_whatsapp"
        interaction = session.get(InteractionRow, inbound.interaction_id)
        assert interaction.relationship_category == "unknown"
        assert interaction.policy_id == "desconhecido"
        assert interaction.policy_version_id
        decision = session.scalar(
            select(AgentDecisionRow).where(AgentDecisionRow.interaction_id == interaction.id)
        )
        assert decision.status == "DRY_RUN"
        assert decision.execution_allowed is False
        assert decision.external_delivery_allowed is False
        assert session.scalar(select(text("count(*)")).select_from(TimerRow).where(TimerRow.interaction_id == interaction.id)) == 0
        assert session.scalar(select(text("count(*)")).select_from(OutboxMessageRow).where(OutboxMessageRow.interaction_id == interaction.id)) == 0
        assert session.scalar(select(text("count(*)")).select_from(AuditEventRow).where(AuditEventRow.event_type == "actor_unresolved")) >= 1


def test_pg_meta_replay_deduplicates(Session, client):
    message_id = f"wamid.replay.{uuid.uuid4()}"
    payload = meta_text_message(message_id)
    first = post_signed(client, payload)
    second = post_signed(client, payload)
    assert first.status_code == 200
    assert second.status_code == 200
    interaction_id, first_decision_id = process_persisted_inbound_decision(Session, message_id)
    replay_interaction_id, replay_decision_id = process_persisted_inbound_decision(Session, message_id)
    assert replay_interaction_id == interaction_id
    assert replay_decision_id == first_decision_id
    with Session() as session:
        assert session.scalar(select(text("count(*)")).select_from(InboundEventRow).where(InboundEventRow.external_event_id == message_id)) == 1
        assert session.scalar(select(text("count(*)")).select_from(InteractionRow).where(InteractionRow.id == interaction_id)) == 1
        assert session.scalar(select(text("count(*)")).select_from(AgentDecisionRow).where(AgentDecisionRow.event_id == session.scalar(select(InboundEventRow.id).where(InboundEventRow.external_event_id == message_id)))) == 1
        assert session.scalar(select(text("count(*)")).select_from(OutboxMessageRow).where(OutboxMessageRow.interaction_id == interaction_id)) == 0
        assert session.scalar(select(text("count(*)")).select_from(AuditEventRow).where(AuditEventRow.interaction_id == interaction_id, AuditEventRow.event_type == "decision.persisted")) == 1
        assert session.scalar(select(text("count(*)")).select_from(AuditEventRow).where(AuditEventRow.event_type == "duplicate_replay")) >= 1


def test_pg_meta_concurrent_duplicate_request(Session, client):
    message_id = f"wamid.race.{uuid.uuid4()}"
    payload = meta_text_message(message_id)
    responses = []

    def run():
        responses.append(post_signed(client, payload))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(response.status_code for response in responses) == [200, 200]
    with Session() as session:
        assert session.scalar(select(text("count(*)")).select_from(InboundEventRow).where(InboundEventRow.external_event_id == message_id)) == 1


def test_pg_meta_batch_two_messages(Session, client):
    response = post_signed(client, meta_batch())
    assert response.status_code == 200
    assert response.json()["processed"] == 2
    with Session() as session:
        assert session.scalar(select(text("count(*)")).select_from(InboundEventRow).where(InboundEventRow.external_event_id.in_(["wamid.synthetic.a", "wamid.synthetic.b"]))) == 2


def test_pg_meta_known_actor_binding_resolves_policy(Session, client):
    sender = "15550000999"
    message_id = f"wamid.binding.{uuid.uuid4()}"
    with Session() as session:
        upsert_actor_binding(
            session,
            source="meta_whatsapp",
            external_actor_id=sender,
            actor_key="contact_mae",
            display_name="Mae Binding",
            actor_category="family_core",
        )
        session.commit()
    response = post_signed(client, meta_text_message(message_id, sender=sender))
    assert response.status_code == 200
    process_persisted_inbound_decision(Session, message_id)
    with Session() as session:
        inbound = session.scalars(select(InboundEventRow).where(InboundEventRow.external_event_id == message_id)).one()
        interaction = session.get(InteractionRow, inbound.interaction_id)
        assert interaction.contact_id == "contact_mae"
        assert interaction.policy_id == "mae"
        assert session.scalar(select(text("count(*)")).select_from(AuditEventRow).where(AuditEventRow.event_type == "actor_resolved")) >= 1


def test_pg_meta_conflict_acks_without_duplicate(Session, client):
    message_id = f"wamid.conflict.{uuid.uuid4()}"
    assert post_signed(client, meta_text_message(message_id, text="first")).status_code == 200
    response = post_signed(client, meta_text_message(message_id, text="changed"))
    assert response.status_code == 200
    assert response.json()["conflicts"] == 1
    with Session() as session:
        assert session.scalar(select(text("count(*)")).select_from(InboundEventRow).where(InboundEventRow.external_event_id == message_id)) == 1
        assert session.scalar(select(text("count(*)")).select_from(AuditEventRow).where(AuditEventRow.event_type == "duplicate_payload_conflict")) >= 1


def test_pg_meta_invalid_signature_persists_no_inbound(Session, client):
    message_id = f"wamid.invalidsig.{uuid.uuid4()}"
    raw = body(meta_text_message(message_id))
    response = client.post(
        "/api/v1/ingress/meta/whatsapp/webhook",
        content=raw,
        headers=signed_headers(raw, "wrong-secret"),
    )
    assert response.status_code == 401
    with Session() as session:
        assert session.scalar(select(text("count(*)")).select_from(InboundEventRow).where(InboundEventRow.external_event_id == message_id)) == 0


def test_pg_meta_status_callback_not_interaction(Session, client):
    message_id = f"wamid.status.{uuid.uuid4()}"
    payload = meta_status(message_id)
    payload["entry"][0]["changes"][0]["value"]["statuses"][0].update(
        {
            "status": "failed",
            "errors": [
                {
                    "code": 131000,
                    "error_subcode": 2494010,
                    "title": "Message undeliverable",
                    "message": "The message could not be delivered",
                    "error_data": {
                        "details": "Recipient unavailable",
                        "access_token": "do-not-persist",
                    },
                    "fbtrace_id": "trace-1",
                    "Authorization": "Bearer do-not-persist",
                },
                {"code": 131026, "message": "Second diagnostic"},
            ],
        }
    )
    response = post_signed(client, payload)
    assert response.status_code == 200
    assert response.json()["processed"] == 0
    with Session() as session:
        assert session.scalar(
            select(text("count(*)"))
            .select_from(InboundEventRow)
            .where(InboundEventRow.external_event_id == message_id)
        ) == 0
        rows = session.scalars(
            select(AuditEventRow).where(AuditEventRow.event_type == "meta_status_received")
        ).all()
        message_hash = hashlib.sha256(message_id.encode()).hexdigest()
        audit_row = next(
            row
            for row in rows
            if row.payload.get("provider_message_id_hash") == message_hash
        )
        assert audit_row.payload == {
            "provider_message_id_hash": message_hash,
            "status": "failed",
            "timestamp": "1723060802",
            "errors_present": True,
            "error_fingerprint": audit_row.payload["error_fingerprint"],
        }
        assert "do-not-persist" not in str(audit_row.payload)


def test_pg_unrelated_integrity_error_cannot_produce_false_status_ack(
    Session, client, monkeypatch
):
    message_id = f"wamid.flush-failure.{uuid.uuid4()}"
    with Session() as session:
        audit(
            session,
            None,
            "remediation_integrity_sentinel",
            {"safe": True},
            origin="test",
        )
        session.commit()
        sentinel_id = session.scalar(
            select(AuditEventRow.id).where(
                AuditEventRow.event_type == "remediation_integrity_sentinel"
            )
        )
    original_new_id = repository.new_id
    monkeypatch.setattr(repository, "new_id", lambda: sentinel_id)
    response = post_signed(client, meta_status(message_id))
    assert response.status_code == 503
    with Session() as session:
        assert session.scalar(
            select(text("count(*)")).select_from(MetaCallbackInboxRow).where(
                MetaCallbackInboxRow.provider_message_id == message_id
            )
        ) == 0

    monkeypatch.setattr(repository, "new_id", original_new_id)
    retry = post_signed(client, meta_status(message_id))
    assert retry.status_code == 200
    with Session() as session:
        assert session.scalar(
            select(text("count(*)")).select_from(MetaCallbackInboxRow).where(
                MetaCallbackInboxRow.provider_message_id == message_id
            )
        ) == 1


def test_pg_deferred_commit_failure_returns_non_2xx_and_retry_converges(
    Session, client
):
    message_id = f"wamid.commit-failure.{uuid.uuid4()}"
    with Session() as session:
        session.execute(
            text(
                "CREATE FUNCTION test_fail_meta_inbox_commit() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION "
                "'synthetic deferred commit failure'; END $$"
            )
        )
        session.execute(
            text(
                "CREATE CONSTRAINT TRIGGER test_meta_inbox_commit_failure "
                "AFTER INSERT ON meta_callback_inbox DEFERRABLE INITIALLY DEFERRED "
                "FOR EACH ROW EXECUTE FUNCTION test_fail_meta_inbox_commit()"
            )
        )
        session.commit()
    try:
        response = post_signed(client, meta_status(message_id))
        assert response.status_code == 503
        with Session() as session:
            assert session.scalar(
                select(text("count(*)")).select_from(MetaCallbackInboxRow).where(
                    MetaCallbackInboxRow.provider_message_id == message_id
                )
            ) == 0
    finally:
        with Session() as session:
            session.execute(
                text(
                    "DROP TRIGGER IF EXISTS test_meta_inbox_commit_failure "
                    "ON meta_callback_inbox"
                )
            )
            session.execute(text("DROP FUNCTION IF EXISTS test_fail_meta_inbox_commit()"))
            session.commit()
    retry = post_signed(client, meta_status(message_id))
    assert retry.status_code == 200
    with Session() as session:
        assert session.scalar(
            select(text("count(*)")).select_from(MetaCallbackInboxRow).where(
                MetaCallbackInboxRow.provider_message_id == message_id
            )
        ) == 1


def test_pg_provider_gate_timeout_returns_non_2xx_without_partial_receipt(
    Session, client
):
    message_id = f"wamid.lock-timeout.{uuid.uuid4()}"
    with Session() as locker, locker.begin():
        acquire_meta_provider_gate(locker, message_id)
        response = post_signed(client, meta_status(message_id))
        assert response.status_code == 503
        with Session() as observer:
            assert observer.scalar(
                select(text("count(*)")).select_from(MetaCallbackInboxRow).where(
                    MetaCallbackInboxRow.provider_message_id == message_id
                )
            ) == 0
    retry = post_signed(client, meta_status(message_id))
    assert retry.status_code == 200


def test_pg_connection_loss_before_commit_is_non_2xx_and_retry_is_idempotent(
    Session, client
):
    message_id = f"wamid.connection-loss.{uuid.uuid4()}"
    engine = Session.kw["bind"]
    FaultSession = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def terminate_after_flush(session, _flush_context):
        if session.info.get("terminated"):
            return
        if any(isinstance(row, MetaCallbackInboxRow) for row in session.new):
            session.info["terminated"] = True
            backend_pid = session.scalar(text("SELECT pg_backend_pid()"))
            with engine.connect() as killer:
                killer.execute(
                    text("SELECT pg_terminate_backend(:pid)"), {"pid": backend_pid}
                )
                killer.commit()

    event.listen(FaultSession, "after_flush", terminate_after_flush)
    app.dependency_overrides[get_callback_session_factory] = lambda: FaultSession
    try:
        response = post_signed(client, meta_status(message_id))
        assert response.status_code == 503
    finally:
        event.remove(FaultSession, "after_flush", terminate_after_flush)
        app.dependency_overrides[get_callback_session_factory] = lambda: Session
    with Session() as session:
        assert session.scalar(
            select(text("count(*)")).select_from(MetaCallbackInboxRow).where(
                MetaCallbackInboxRow.provider_message_id == message_id
            )
        ) == 0
    retry = post_signed(client, meta_status(message_id))
    assert retry.status_code == 200
    with Session() as session:
        assert session.scalar(
            select(text("count(*)")).select_from(MetaCallbackInboxRow).where(
                MetaCallbackInboxRow.provider_message_id == message_id
            )
        ) == 1
