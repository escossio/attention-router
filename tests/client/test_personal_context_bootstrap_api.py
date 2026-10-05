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


def _row(state="QUEUED", source_kind="WHATSAPP_TEXT"):
    now = datetime(2026, 10, 3, tzinfo=UTC)
    return SimpleNamespace(
        id="pcb_test",
        source_kind=source_kind,
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


class FakeGmailService:
    def __init__(self):
        self.calls = []

    def create_and_queue(
        self,
        session,
        *,
        session_token,
        consent_ref,
        processing_budget=None,
    ):
        self.calls.append(
            (
                "create_gmail",
                session_token,
                consent_ref,
                processing_budget,
            )
        )
        return _row(source_kind="GMAIL_TEXT")


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


def _client(session, *, with_gmail=False):
    service = FakeService()
    gmail_service = FakeGmailService() if with_gmail else None

    def get_session():
        yield session

    app = FastAPI()
    app.include_router(
        build_personal_context_bootstrap_router(
            get_session=get_session,
            service=service,
            gmail_service=gmail_service,
        )
    )
    return TestClient(app), service, gmail_service


def test_bootstrap_api_create_queues_owner_request_without_identity_shortcuts(session):
    client, service, _gmail_service = _client(session)

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
    client, service, _gmail_service = _client(session)

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


def test_gmail_bootstrap_api_uses_same_client_session_without_tenant_override(session):
    client, _service, gmail_service = _client(
        session,
        with_gmail=True,
    )

    response = client.post(
        "/api/v1/personal-context/bootstrap/gmail",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "consent_ref": "gmail-consent-ui-001",
            "processing_budget": {"max_total_messages": 25},
        },
    )

    assert response.status_code == 201
    assert response.json()["source_kind"] == "GMAIL_TEXT"
    assert response.json()["state"] == "QUEUED"
    assert gmail_service.calls == [
        (
            "create_gmail",
            TOKEN,
            "gmail-consent-ui-001",
            {"max_total_messages": 25},
        )
    ]

    forbidden = client.post(
        "/api/v1/personal-context/bootstrap/gmail",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "consent_ref": "gmail-consent-ui-001",
            "tenant_id": "client-selected-tenant",
        },
    )
    assert forbidden.status_code == 422


def test_gmail_bootstrap_missing_connection_is_conflict(session, monkeypatch):
    from attention_router.application.gmail_bootstrap import (
        GmailBootstrapConnectionRequired,
    )

    client, _service, gmail_service = _client(session, with_gmail=True)

    def missing_connection(*args, **kwargs):
        raise GmailBootstrapConnectionRequired()

    monkeypatch.setattr(gmail_service, "create_and_queue", missing_connection)
    response = client.post(
        "/api/v1/personal-context/bootstrap/gmail",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"consent_ref": "gmail-consent-ui-connection"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "GMAIL_BOOTSTRAP_CONNECTION_REQUIRED"


def test_gmail_bootstrap_readonly_upgrade_is_conflict(session, monkeypatch):
    from attention_router.application.gmail_bootstrap import (
        GmailBootstrapReadonlyRequired,
    )

    client, _service, gmail_service = _client(session, with_gmail=True)

    def readonly_required(*args, **kwargs):
        raise GmailBootstrapReadonlyRequired()

    monkeypatch.setattr(gmail_service, "create_and_queue", readonly_required)
    response = client.post(
        "/api/v1/personal-context/bootstrap/gmail",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"consent_ref": "gmail-consent-ui-readonly"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "GMAIL_BOOTSTRAP_READONLY_REQUIRED"


def test_bootstrap_lifecycle_error_detail_is_never_echoed(session, monkeypatch):
    client, service, _gmail_service = _client(session)
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
