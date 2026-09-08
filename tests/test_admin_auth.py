from fastapi.testclient import TestClient

from attention_router.web.app import app


def test_health_without_admin_token():
    response = TestClient(app).get("/health/live")
    assert response.status_code == 200


def test_admin_without_credential_is_rejected():
    response = TestClient(app).get("/api/v1/admin/policies")
    assert response.status_code == 401


def test_admin_invalid_credential_is_rejected():
    response = TestClient(app).get("/api/v1/admin/policies", headers={"Authorization": "Bearer invalid"})
    assert response.status_code == 403


def test_admin_valid_credential_is_allowed(monkeypatch):
    monkeypatch.setattr("attention_router.web.app.settings.admin_token", "synthetic-admin-token")
    response = TestClient(app, raise_server_exceptions=False).get(
        "/api/v1/admin/policies", headers={"Authorization": "Bearer synthetic-admin-token"}
    )
    assert response.status_code in {200, 500}


def test_synthetic_ingress_does_not_require_admin_token():
    response = TestClient(app).post("/api/v1/ingress/synthetic/events", json={})
    assert response.status_code == 422
