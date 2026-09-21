from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from attention_router.api.v1.gmail_connection import (
    build_gmail_connection_router,
)
from attention_router.application.gmail_connection import (
    GMAIL_METADATA_SCOPE,
    GmailConnectionView,
)


TOKEN = "cst_" + "t" * 43


class FakeService:
    def __init__(self):
        self.calls = []

    def status(self, session, *, session_token):
        self.calls.append(("status", session_token))
        return GmailConnectionView(
            "CONNECTED",
            "paz_synthetic",
            (GMAIL_METADATA_SCOPE,),
        )

    def connect(self, session, *, session_token, authorization_code):
        self.calls.append(
            ("connect", session_token, authorization_code)
        )
        return GmailConnectionView(
            "CONNECTED",
            "paz_synthetic",
            (GMAIL_METADATA_SCOPE,),
        )

    def disconnect(self, session, *, session_token):
        self.calls.append(("disconnect", session_token))
        return GmailConnectionView("DISCONNECTED", None, ())


def _client(session):
    service = FakeService()

    def get_session():
        yield session

    app = FastAPI()
    app.include_router(
        build_gmail_connection_router(
            get_session=get_session,
            service=service,
        )
    )
    return TestClient(app), service


def test_gmail_product_api_accepts_only_auth_code_and_client_session(session):
    client, service = _client(session)

    response = client.post(
        "/api/v1/integrations/gmail",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"authorization_code": "server-auth-code"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "contract_version": "1",
        "provider": "GOOGLE",
        "product": "GMAIL",
        "status": "CONNECTED",
        "installation_id": "paz_synthetic",
        "granted_scopes": [GMAIL_METADATA_SCOPE],
    }
    assert service.calls == [
        ("connect", TOKEN, "server-auth-code")
    ]

    forbidden = client.post(
        "/api/v1/integrations/gmail",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "authorization_code": "server-auth-code",
            "tenant_id": "client-selected",
        },
    )
    assert forbidden.status_code == 422


def test_gmail_product_status_and_disconnect_use_same_client_session(session):
    client, service = _client(session)

    status = client.get(
        "/api/v1/integrations/gmail",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    disconnected = client.delete(
        "/api/v1/integrations/gmail",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert status.status_code == 200
    assert status.json()["status"] == "CONNECTED"
    assert disconnected.status_code == 200
    assert disconnected.json()["status"] == "DISCONNECTED"
    assert service.calls == [
        ("status", TOKEN),
        ("disconnect", TOKEN),
    ]


def test_gmail_product_api_never_echoes_authorization_code(session):
    client, _service = _client(session)
    secret = "super-sensitive-server-auth-code"

    response = client.post(
        "/api/v1/integrations/gmail",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"authorization_code": secret},
    )

    assert response.status_code == 200
    assert secret not in response.text
