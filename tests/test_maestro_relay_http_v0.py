"""Offline HTTP contract for the opt-in MAESTRO receiver (no PostgreSQL)."""
from fastapi.testclient import TestClient
import pytest

from attention_router.config import settings
from attention_router.maestro.admission import MaestroAdmission
from attention_router.web import maestro_relay_ingress as ingress


@pytest.fixture
def client(monkeypatch):
    app = ingress.create_maestro_tls_app()
    app.dependency_overrides[ingress.get_maestro_relay_session_factory] = lambda: object()
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


def test_default_is_fail_closed(client):
    assert settings.maestro_relay_ingress_enabled is False
    response = client.post(
        ingress.MAESTRO_RELAY_PATH,
        json={"data": "synthetic"},
        headers={"Authorization": "Bearer fake", "Idempotency-Key": "zabbix:1"},
    )
    assert response.status_code == 503


def test_unauthenticated_request_is_not_dispatched(client, monkeypatch):
    monkeypatch.setattr(settings, "maestro_relay_ingress_enabled", True)
    monkeypatch.setattr(ingress, "admit_maestro_relay",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("unexpected dispatch")))
    response = client.post(ingress.MAESTRO_RELAY_PATH, json={"signal": {}},
                           headers={"Idempotency-Key": "zabbix:1"})
    assert response.status_code == 401


def test_duplicate_and_accepted_http_are_translated_only_after_admission(client, monkeypatch):
    monkeypatch.setattr(settings, "maestro_relay_ingress_enabled", True)
    calls = []
    def fake_admit(_factory, token, body, **kwargs):
        calls.append((token, kwargs["idempotency_key"], body))
        return MaestroAdmission("ACCEPTED" if len(calls) == 1 else "DUPLICATE",
                                "synthetic-receipt-1", 1)
    monkeypatch.setattr(ingress, "admit_maestro_relay", fake_admit)
    headers = {"Authorization": "Bearer synthetic-token", "Idempotency-Key": "zabbix:1"}
    first = client.post(ingress.MAESTRO_RELAY_PATH, json={"test": "synthetic"}, headers=headers)
    assert first.status_code == 202
    assert first.json()["receipt_id"] == "synthetic-receipt-1"
    replay = client.post(ingress.MAESTRO_RELAY_PATH, json={"test": "synthetic"}, headers=headers)
    assert replay.status_code == 200
    assert replay.json()["status"] == "DUPLICATE"
    assert len(calls) == 2


def test_rejected_authority_errors_do_not_echo_request(client, monkeypatch):
    monkeypatch.setattr(settings, "maestro_relay_ingress_enabled", True)
    monkeypatch.setattr(ingress, "admit_maestro_relay",
                        lambda *_args, **_kwargs: MaestroAdmission("FORBIDDEN"))
    headers = {"Authorization": "Bearer synthetic-token", "Idempotency-Key": "zabbix:1"}
    response = client.post(ingress.MAESTRO_RELAY_PATH, json={"secret_data": "not-echoed"}, headers=headers)
    assert response.status_code == 403
    assert "not-echoed" not in response.text


def test_invalid_media_type_rejected_before_admission(client, monkeypatch):
    monkeypatch.setattr(settings, "maestro_relay_ingress_enabled", True)
    response = client.post(
        ingress.MAESTRO_RELAY_PATH, content=b"{}",
        headers={"Authorization": "Bearer fake", "Idempotency-Key": "zabbix:1",
                 "Content-Type": "text/plain"},
    )
    assert response.status_code == 415


def test_no_query_string_authority(client, monkeypatch):
    monkeypatch.setattr(settings, "maestro_relay_ingress_enabled", True)
    response = client.post(
        ingress.MAESTRO_RELAY_PATH + "?tenant=synthetic", json={},
        headers={"Authorization": "Bearer fake", "Idempotency-Key": "zabbix:1"},
    )
    assert response.status_code == 400


def test_http_scheme_never_reaches_admission(monkeypatch):
    app = ingress.create_maestro_tls_app()
    monkeypatch.setattr(settings, "maestro_relay_ingress_enabled", True)
    with TestClient(app, base_url="http://testserver") as client:
        response = client.post(ingress.MAESTRO_RELAY_PATH, json={},
                               headers={"Authorization": "Bearer not-real",
                                        "Idempotency-Key": "zabbix:1"})
        assert response.status_code == 403
        assert response.json()["status"] == "TLS_REQUIRED"
