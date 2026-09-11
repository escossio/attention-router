import hashlib
import hmac
import json
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from attention_router.config import settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import (
    AuditEventRow,
    DecisionRow,
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
    TimerRow,
)
from attention_router.web.internal_ingress_app import app, get_session


pytestmark = pytest.mark.postgres

SECRET = "postgres-internal-ingress-secret-32-bytes"


@pytest.fixture()
def client(Session, monkeypatch):
    monkeypatch.setattr(settings, "internal_ingress_hmac_secret", SECRET)
    # Ingress processing owns its transaction rather than using the HTTP dependency.
    monkeypatch.setattr("attention_router.web.internal_ingress_app.SessionLocal", Session)

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
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def payload(event_id: str, text_value: str = "Mensagem interna PostgreSQL.") -> dict:
    return {
        "schema_version": "1",
        "tenant_id": DEFAULT_TENANT_ID,
        "source": "wwebjs",
        "external_event_id": event_id,
        "event_type": "message",
        "external_actor_id": "wa_actor_5511888877777",
        "channel": "whatsapp",
        "message_type": "text",
        "content": text_value,
        "has_media": False,
        "metadata": {"case": "postgres"},
    }


def headers(body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = hmac.new(SECRET.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"X-Attention-Timestamp": timestamp, "X-Attention-Signature": f"sha256={signature}"}


def post(client: TestClient, data: dict):
    body = json.dumps(data, separators=(",", ":")).encode()
    return client.post("/api/v1/ingress/internal/events", content=body, headers=headers(body))


def test_internal_ingress_postgres_pipeline_and_replay(Session, client):
    event_id = f"internal-{uuid.uuid4()}"
    first = post(client, payload(event_id))
    second = post(client, payload(event_id))
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    interaction_id = first.json()["interaction_id"]
    with Session() as session:
        assert session.scalar(select(text("count(*)")).select_from(InboundEventRow).where(InboundEventRow.tenant_id == DEFAULT_TENANT_ID, InboundEventRow.external_event_id == event_id, InboundEventRow.source == "wwebjs")) == 1
        assert session.scalar(select(text("count(*)")).select_from(InteractionRow).where(InteractionRow.id == interaction_id)) == 1
        assert session.scalar(select(text("count(*)")).select_from(DecisionRow).where(DecisionRow.interaction_id == interaction_id)) == 1
        assert session.scalar(select(text("count(*)")).select_from(TimerRow).where(TimerRow.interaction_id == interaction_id)) == 1
        assert session.scalar(select(text("count(*)")).select_from(OutboxMessageRow).where(OutboxMessageRow.interaction_id == interaction_id)) == 1
        assert session.scalar(select(text("count(*)")).select_from(AuditEventRow).where(AuditEventRow.interaction_id == interaction_id)) > 0


def test_internal_ingress_postgres_conflict(Session, client):
    event_id = f"internal-conflict-{uuid.uuid4()}"
    assert post(client, payload(event_id)).status_code == 200
    assert post(client, payload(event_id, "Texto modificado.")).status_code == 409
    with Session() as session:
        assert session.scalar(select(text("count(*)")).select_from(InboundEventRow).where(InboundEventRow.tenant_id == DEFAULT_TENANT_ID, InboundEventRow.external_event_id == event_id, InboundEventRow.source == "wwebjs")) == 1


def test_internal_ingress_postgres_concurrent_deduplicates(Session, client):
    event_id = f"internal-race-{uuid.uuid4()}"
    responses = []

    def post_event():
        responses.append(post(client, payload(event_id)))

    threads = [threading.Thread(target=post_event) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(response.status_code for response in responses) == [200, 200]
    interaction_ids = {response.json()["interaction_id"] for response in responses}
    assert len(interaction_ids) == 1
    with Session() as session:
        assert session.scalar(select(text("count(*)")).select_from(InboundEventRow).where(InboundEventRow.tenant_id == DEFAULT_TENANT_ID, InboundEventRow.external_event_id == event_id, InboundEventRow.source == "wwebjs")) == 1
        assert session.scalar(select(text("count(*)")).select_from(InteractionRow).where(InteractionRow.id.in_(interaction_ids))) == 1
