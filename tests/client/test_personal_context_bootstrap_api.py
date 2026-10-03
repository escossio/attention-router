from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from attention_router.api.v1.personal_context_bootstrap import (
    build_personal_context_bootstrap_router,
)
from attention_router.application.personal_context_bootstrap import (
    PersonalContextBootstrapError,
)


TOKEN = "cst_" + "b" * 43


def _row(state="QUEUED"):
    now = datetime(2026, 10, 3, tzinfo=UTC)
    return SimpleNamespace(
        id="pcb_test",
        source_kind="WHATSAPP_TEXT",
        state=state,
        requested_control="NONE",
        source_selection={"chat_keys": ["chat-a"]},
        processing_budget={
            "page_size": 50,
            "max_messages_per_chat": 100,
            "max_total_messages": 100,
        },
        progress={},
        created_at=now,
        updated_at=now,
        started_at=None,
        paused_at=None,
        completed_at=None,
        cancelled_at=None,
        failed_at=None,
        failure_summary=None,
    )


class FakeService:
    def __init__(self):
        self.calls = []

    def create_and_queue(
        self,
        session,
        *,
        session_token,
        consent_ref,
        chat_keys=None,
        processing_budget=None,
    ):
        self.calls.append(
            (
                "create",
                session_token,
                consent_ref,
                chat_keys,
                processing_budget,
            )
        )
        return _row()

    def status(self, session, *, session_token, run_id):
        self.calls.append(("status", session_token, run_id))
        return _row()

    def pause(self, session, *, session_token, run_id):
        self.calls.append(("pause", session_token, run_id))
        return _row("PAUSED")

    def resume(self, session, *, session_token, run_id):
        self.calls.append(("resume", session_token, run_id))
        return _row("QUEUED")

    def cancel(self, session, *, session_token, run_id):
        self.calls.append(("cancel", session_token, run_id))
        return _row("CANCELLED")


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


def test_bootstrap_api_create_queues_owner_request_without_identity_shortcuts(session):
    client, service = _client(session)

    response = client.post(
        "/api/v1/personal-context/bootstrap",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "consent_ref": "consent-ui-001",
            "chat_keys": ["chat-a"],
            "processing_budget": {"max_total_messages": 100},
        },
    )

    assert response.status_code == 201
    assert response.json()["state"] == "QUEUED"
    assert response.json()["source_kind"] == "WHATSAPP_TEXT"
    assert service.calls == [
        (
            "create",
            TOKEN,
            "consent-ui-001",
            ["chat-a"],
            {"max_total_messages": 100},
        )
    ]

    forbidden = client.post(
        "/api/v1/personal-context/bootstrap",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "consent_ref": "consent-ui-001",
            "tenant_id": "client-selected-tenant",
        },
    )
    assert forbidden.status_code == 422


def test_bootstrap_status_pause_resume_cancel_use_same_client_session(session):
    client, service = _client(session)

    status_response = client.get(
        "/api/v1/personal-context/bootstrap/pcb_test",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    pause_response = client.post(
        "/api/v1/personal-context/bootstrap/pcb_test/pause",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    resume_response = client.post(
        "/api/v1/personal-context/bootstrap/pcb_test/resume",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    cancel_response = client.post(
        "/api/v1/personal-context/bootstrap/pcb_test/cancel",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert status_response.status_code == 200
    assert pause_response.json()["state"] == "PAUSED"
    assert resume_response.json()["state"] == "QUEUED"
    assert cancel_response.json()["state"] == "CANCELLED"
    assert service.calls == [
        ("status", TOKEN, "pcb_test"),
        ("pause", TOKEN, "pcb_test"),
        ("resume", TOKEN, "pcb_test"),
        ("cancel", TOKEN, "pcb_test"),
    ]


def test_bootstrap_lifecycle_error_detail_is_never_echoed(session, monkeypatch):
    client, service = _client(session)
    provider_detail = "provider-detail-must-not-cross-api-boundary"

    def fail_status(session, *, session_token, run_id):
        raise PersonalContextBootstrapError(provider_detail)

    monkeypatch.setattr(service, "status", fail_status)

    response = client.get(
        "/api/v1/personal-context/bootstrap/pcb_test",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "PERSONAL_CONTEXT_BOOTSTRAP_INVALID"
    }
    assert provider_detail not in response.text
    assert TOKEN not in response.text
