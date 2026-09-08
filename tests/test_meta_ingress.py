from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from attention_router.infrastructure.db import Base
from attention_router.infrastructure.repository import seed_policies
from attention_router.platform.meta_callback_reconciliation import (
    MetaAdmissionResult,
    MetaAdmissionUnavailable,
)
from attention_router.web.ingress_app import app, get_session
from tests.meta_fixtures import (
    APP_SECRET,
    VERIFY_TOKEN,
    body,
    meta_status,
    meta_text_message,
    signed_headers,
)


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with Session() as session:
        seed_policies(session)
        session.commit()

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
        engine.dispose()


def test_meta_webhook_get_valid(monkeypatch, client):
    monkeypatch.setattr("attention_router.web.ingress_app.settings.meta_verify_token", VERIFY_TOKEN)
    response = client.get(
        "/api/v1/ingress/meta/whatsapp/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "abc"},
    )
    assert response.status_code == 200
    assert response.text == "abc"


def test_meta_webhook_get_invalid(monkeypatch, client):
    monkeypatch.setattr("attention_router.web.ingress_app.settings.meta_verify_token", VERIFY_TOKEN)
    response = client.get(
        "/api/v1/ingress/meta/whatsapp/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "abc"},
    )
    assert response.status_code == 403


def test_meta_post_missing_signature(monkeypatch, client):
    monkeypatch.setattr("attention_router.web.ingress_app.settings.meta_app_secret", APP_SECRET)
    response = client.post("/api/v1/ingress/meta/whatsapp/webhook", content=body(meta_text_message()))
    assert response.status_code == 401


def test_meta_post_malformed_signature(monkeypatch, client):
    monkeypatch.setattr("attention_router.web.ingress_app.settings.meta_app_secret", APP_SECRET)
    response = client.post(
        "/api/v1/ingress/meta/whatsapp/webhook",
        content=body(meta_text_message()),
        headers={"X-Hub-Signature-256": "bad"},
    )
    assert response.status_code == 401


def test_meta_post_wrong_signature(monkeypatch, client):
    monkeypatch.setattr("attention_router.web.ingress_app.settings.meta_app_secret", APP_SECRET)
    response = client.post(
        "/api/v1/ingress/meta/whatsapp/webhook",
        content=body(meta_text_message()),
        headers=signed_headers(body(meta_text_message()), "wrong-secret"),
    )
    assert response.status_code == 401


def test_meta_post_body_size(monkeypatch, client):
    monkeypatch.setattr("attention_router.web.ingress_app.settings.meta_app_secret", APP_SECRET)
    monkeypatch.setattr("attention_router.web.ingress_app.settings.meta_max_webhook_body_bytes", 2)
    raw = body(meta_text_message())
    response = client.post(
        "/api/v1/ingress/meta/whatsapp/webhook",
        content=raw,
        headers=signed_headers(raw),
    )
    assert response.status_code == 413


def test_meta_post_parses_without_dispatch(monkeypatch, client):
    monkeypatch.setattr("attention_router.web.ingress_app.settings.meta_app_secret", APP_SECRET)
    monkeypatch.setattr("attention_router.web.ingress_app.settings.meta_webhook_dispatch_enabled", False)
    raw = body(meta_text_message())
    response = client.post(
        "/api/v1/ingress/meta/whatsapp/webhook", content=raw, headers=signed_headers(raw)
    )
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok", "processed": 0, "duplicates": 0, "conflicts": 0, "parsed": 1, "normalized": 1
    }


def test_meta_shadow_replay_is_idempotent(monkeypatch, client):
    monkeypatch.setattr("attention_router.web.ingress_app.settings.meta_app_secret", APP_SECRET)
    monkeypatch.setattr("attention_router.web.ingress_app.settings.meta_webhook_dispatch_enabled", False)
    raw = body(meta_text_message("wamid.shadow.replay"))
    first = client.post("/api/v1/ingress/meta/whatsapp/webhook", content=raw, headers=signed_headers(raw))
    second = client.post("/api/v1/ingress/meta/whatsapp/webhook", content=raw, headers=signed_headers(raw))
    assert first.status_code == second.status_code == 200
    assert first.json()["normalized"] == 1
    assert second.json()["duplicates"] == 1
    assert second.json()["normalized"] == 0


def test_meta_status_without_durable_admission_returns_retriable_non_2xx(
    monkeypatch, client
):
    monkeypatch.setattr("attention_router.web.ingress_app.settings.meta_app_secret", APP_SECRET)

    def unavailable(*args, **kwargs):
        raise MetaAdmissionUnavailable("test admission failure")

    monkeypatch.setattr(
        "attention_router.web.ingress_app.admit_meta_callback_evidence", unavailable
    )
    raw = body(meta_status("wamid.admission.unavailable"))
    response = client.post(
        "/api/v1/ingress/meta/whatsapp/webhook",
        content=raw,
        headers=signed_headers(raw),
    )
    assert response.status_code == 503


def test_meta_status_persisted_then_reconciliation_deferred_is_acknowledged(
    monkeypatch, client
):
    monkeypatch.setattr("attention_router.web.ingress_app.settings.meta_app_secret", APP_SECRET)
    monkeypatch.setattr(
        "attention_router.web.ingress_app.admit_meta_callback_evidence",
        lambda *args, **kwargs: MetaAdmissionResult(
            reconciliation_id="recon-http-deferred",
            scope_accepted=True,
            evidence_persisted=True,
            duplicate=False,
            admissible=True,
        ),
    )

    def deferred(*args, **kwargs):
        raise TimeoutError("test graph contention")

    monkeypatch.setattr(
        "attention_router.web.ingress_app.reconcile_meta_callback_outcome", deferred
    )
    raw = body(meta_status("wamid.admission.persisted"))
    response = client.post(
        "/api/v1/ingress/meta/whatsapp/webhook",
        content=raw,
        headers=signed_headers(raw),
    )
    assert response.status_code == 200
    assert response.json()["status_admitted"] == 1
    assert response.json()["reconciliation_deferred"] == 1


def test_ingress_listener_has_no_admin_or_docs(client):
    assert client.get("/api/v1/admin/policies").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.post("/sim/interactions").status_code == 404
