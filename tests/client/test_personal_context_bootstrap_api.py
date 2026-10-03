from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from attention_router.api.v1.personal_context_bootstrap import (
    build_personal_context_bootstrap_router,
)
from attention_router.application.personal_context_bootstrap_product import (
    BootstrapProductView,
)


TOKEN = "cst_" + "t" * 43
STAMP = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


class FakeService:
    def __init__(self):
        self.calls = []

    @staticmethod
    def view(run_id="run-1", state="QUEUED"):
        return BootstrapProductView(
            run_id=run_id,
            state=state,
            source_kind="WHATSAPP_TEXT",
            source_account="default",
            progress={},
            created_at=STAMP,
            updated_at=STAMP,
            started_at=None,
            paused_at=None,
            completed_at=None,
            cancelled_at=None,
            failed_at=None,
            failure_summary=None,
        )

    def start(self, session, **kwargs):
        self.calls.append(("start", kwargs))
        return self.view()

    def status(self, session, **kwargs):
        self.calls.append(("status", kwargs))
        return self.view(run_id=kwargs["run_id"])

    def list_recent(self, session, **kwargs):
        self.calls.append(("list", kwargs))
        return (self.view(),)

    def control(self, session, **kwargs):
        self.calls.append(("control", kwargs))
        return self.view(run_id=kwargs["run_id"], state=kwargs["action"])


def _client(session):
    service = FakeService()

    def get_session():
        yield session

    app = FastAPI()
    app.include_router(
        build_personal_context_bootstrap_router(
            get_session=get_session,
            service=service,
        )
    )
    return TestClient(app), service


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_api_start_accepts_selection_but_not_authority_fields(session):
    client, service = _client(session)
    response = client.post(
        "/api/v1/client/personal-context/bootstrap",
        headers=auth(),
        json={
            "client_request_id": "request-1",
            "chat_keys": ["chat-a"],
        },
    )
    assert response.status_code == 200
    assert response.json()["state"] == "QUEUED"
    call = service.calls[-1][1]
    assert call["session_token"] == TOKEN
    assert call["client_request_id"] == "request-1"
    assert call["chat_keys"] == ("chat-a",)

    for forbidden in (
        "tenant_id",
        "human_identity_id",
        "represented_owner_actor_key",
        "source_account",
        "source_kind",
        "consent_ref",
    ):
        bad = client.post(
            "/api/v1/client/personal-context/bootstrap",
            headers=auth(),
            json={
                "client_request_id": "request-2",
                forbidden: "forged",
            },
        )
        assert bad.status_code == 422


def test_api_status_list_and_controls_use_same_bearer(session):
    client, service = _client(session)
    status_response = client.get(
        "/api/v1/client/personal-context/bootstrap/run-x",
        headers=auth(),
    )
    list_response = client.get(
        "/api/v1/client/personal-context/bootstrap?limit=5",
        headers=auth(),
    )
    control_response = client.post(
        "/api/v1/client/personal-context/bootstrap/run-x/control",
        headers=auth(),
        json={"action": "PAUSE"},
    )

    assert status_response.status_code == 200
    assert list_response.status_code == 200
    assert control_response.status_code == 200
    assert [call[0] for call in service.calls] == [
        "status",
        "list",
        "control",
    ]
    assert all(
        call[1]["session_token"] == TOKEN
        for call in service.calls
    )
