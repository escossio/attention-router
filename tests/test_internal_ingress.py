import hashlib
import hmac
import json
import time
import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from attention_router.config import settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import now_utc
from attention_router.infrastructure.models import TenantRow
from attention_router.web.internal_ingress_app import (
    app,
    get_session,
    health_engine,
    internal_ingress_executor,
)


SECRET = "unit-test-internal-ingress-secret-32-bytes"


def teardown_function():
    app.dependency_overrides.clear()


def payload(event_id: str | None = None, text: str = "Mensagem interna sintética.") -> dict:
    return {
        "schema_version": "1",
        "tenant_id": DEFAULT_TENANT_ID,
        "source": "wwebjs",
        "external_event_id": event_id or f"int-{uuid.uuid4()}",
        "event_type": "message",
        "external_actor_id": "wa_actor_5500000000029",
        "channel": "whatsapp",
        "message_type": "text",
        "content": text,
        "has_media": False,
        "metadata": {"fixture": "unit"},
    }


def owner_command_payload(event_id: str) -> dict:
    data = payload(event_id, "espera 60")
    data.update(
        {
            "event_origin": "OWNER_COMMAND",
            "owner_authenticated": True,
            "metadata": {
                "from_me": True,
                "owner_self_chat": True,
                "from_me_classification": "OWNER_COMMAND",
                "final_from_me_classification": "OWNER_COMMAND",
            },
        }
    )
    return data


def signed_headers(body: bytes, secret: str = SECRET, timestamp: int | None = None) -> dict[str, str]:
    timestamp = timestamp or int(time.time())
    signature = hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"X-Attention-Timestamp": str(timestamp), "X-Attention-Signature": f"sha256={signature}"}


def client(session, monkeypatch):
    monkeypatch.setattr(settings, "internal_ingress_hmac_secret", SECRET)
    # The executor creates its own session; a FastAPI dependency override alone
    # does not reach that boundary. Share only this disposable fixture engine.
    monkeypatch.setattr(
        "attention_router.web.internal_ingress_app.SessionLocal",
        sessionmaker(bind=session.get_bind(), expire_on_commit=False, future=True),
    )

    def override():
        yield session

    app.dependency_overrides[get_session] = override
    return TestClient(app)


def post(client: TestClient, data: dict, headers: dict[str, str] | None = None):
    body = json.dumps(data, separators=(",", ":")).encode()
    return client.post("/api/v1/ingress/internal/events", content=body, headers=headers or signed_headers(body))


def test_internal_ingress_valid_hmac_accepts_payload(session, monkeypatch):
    c = client(session, monkeypatch)
    response = post(c, payload())
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert response.json()["interaction_id"]


def test_internal_ingress_requires_explicit_tenant(session, monkeypatch):
    c = client(session, monkeypatch)
    data = payload()
    data.pop("tenant_id")
    assert post(c, data).status_code == 422


def test_internal_ingress_rejects_unknown_tenant(session, monkeypatch):
    c = client(session, monkeypatch)
    data = payload()
    data["tenant_id"] = f"missing-{uuid.uuid4()}"
    response = post(c, data)
    assert response.status_code == 403
    assert response.json()["detail"] == "tenant unavailable"


def test_internal_ingress_rejects_inactive_tenant(session, monkeypatch):
    tenant_id = f"inactive-{uuid.uuid4()}"
    stamp = now_utc()
    session.add(TenantRow(
        id=tenant_id,
        slug=tenant_id,
        name="Inactive synthetic tenant",
        status="SUSPENDED",
        created_at=stamp,
        updated_at=stamp,
    ))
    session.commit()
    c = client(session, monkeypatch)
    data = payload()
    data["tenant_id"] = tenant_id
    assert post(c, data).status_code == 403


def test_internal_ingress_invalid_hmac_rejected(session, monkeypatch):
    c = client(session, monkeypatch)
    body = json.dumps(payload()).encode()
    response = c.post("/api/v1/ingress/internal/events", content=body, headers=signed_headers(body, "wrong-secret"))
    assert response.status_code == 401


def test_internal_ingress_missing_signature_rejected(session, monkeypatch):
    c = client(session, monkeypatch)
    response = c.post("/api/v1/ingress/internal/events", json=payload())
    assert response.status_code == 401


def test_internal_ingress_expired_timestamp_rejected(session, monkeypatch):
    c = client(session, monkeypatch)
    body = json.dumps(payload()).encode()
    response = c.post(
        "/api/v1/ingress/internal/events",
        content=body,
        headers=signed_headers(body, timestamp=int(time.time()) - settings.internal_ingress_max_skew_seconds - 10),
    )
    assert response.status_code == 401


def test_internal_ingress_future_timestamp_rejected(session, monkeypatch):
    c = client(session, monkeypatch)
    body = json.dumps(payload()).encode()
    response = c.post(
        "/api/v1/ingress/internal/events",
        content=body,
        headers=signed_headers(body, timestamp=int(time.time()) + settings.internal_ingress_max_skew_seconds + 10),
    )
    assert response.status_code == 401


def test_internal_ingress_schema_invalid(session, monkeypatch):
    c = client(session, monkeypatch)
    data = payload()
    data["unexpected"] = True
    assert post(c, data).status_code == 422


def test_internal_ingress_rejects_owner_command_without_final_classification(
    session,
    monkeypatch,
):
    c = client(session, monkeypatch)
    data = owner_command_payload("owner-command-no-final")
    data["metadata"].pop("final_from_me_classification")
    assert post(c, data).status_code == 422


def test_internal_ingress_rejects_owner_command_without_initial_classification(
    session,
    monkeypatch,
):
    c = client(session, monkeypatch)
    data = owner_command_payload("owner-command-no-initial")
    data["metadata"].pop("from_me_classification")
    assert post(c, data).status_code == 422


def test_internal_ingress_rejects_other_source(session, monkeypatch):
    c = client(session, monkeypatch)
    data = payload()
    data["source"] = "meta_whatsapp"
    assert post(c, data).status_code == 422


def test_internal_ingress_replay_is_idempotent(session, monkeypatch):
    c = client(session, monkeypatch)
    data = payload()
    first = post(c, data)
    session.commit()
    second = post(c, data)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert first.json()["interaction_id"] == second.json()["interaction_id"]


def test_internal_ingress_same_id_different_payload_conflicts(session, monkeypatch):
    c = client(session, monkeypatch)
    event_id = f"conflict-{uuid.uuid4()}"
    assert post(c, payload(event_id)).status_code == 200
    session.commit()
    conflict = post(c, payload(event_id, "Mensagem alterada."))
    assert conflict.status_code == 409


def test_internal_ingress_admin_docs_meta_and_synthetic_are_404(session, monkeypatch):
    c = client(session, monkeypatch)
    assert c.get("/api/v1/admin/policies").status_code == 404
    assert c.get("/docs").status_code == 404
    assert c.get("/openapi.json").status_code == 404
    assert c.get("/api/v1/ingress/meta/whatsapp/webhook").status_code == 404
    assert c.post("/api/v1/ingress/synthetic/events").status_code == 404


def test_internal_ingress_uses_dedicated_executor_for_blocking_work():
    route = next(route for route in app.routes if getattr(route, "path", None) == "/api/v1/ingress/internal/events")
    assert "run_in_executor" in route.endpoint.__code__.co_names
    assert getattr(internal_ingress_executor, "_max_workers") == 16


def test_internal_ingress_ready_uses_separate_unpooled_health_engine():
    assert health_engine.pool.__class__.__name__ == "NullPool"
