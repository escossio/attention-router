from contextlib import nullcontext
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from attention_router.api.v1.client_approval import build_client_approval_router
from attention_router.application.client_approval import (
    ClientApprovalAuthorityRejected,
    ClientApprovalConflict,
    ClientApprovalView,
)


NOW = datetime(2026, 9, 22, 6, 45, tzinfo=UTC)
PENDING = "/api/v1/client/approvals/pending"
DETAIL = "/api/v1/client/approvals/apr_test"
DECISION = "/api/v1/client/approvals/apr_test/decision"


class FakeSession:
    def begin_nested(self):
        return nullcontext()


class FakeService:
    def __init__(self):
        self.error = None
        self.calls = []

    def list_pending(self, session, **kwargs):
        self.calls.append(("list", session, kwargs))
        if self.error:
            raise self.error
        return (approval(),)

    def get(self, session, **kwargs):
        self.calls.append(("get", session, kwargs))
        if self.error:
            raise self.error
        return approval()

    def decide(self, session, **kwargs):
        self.calls.append(("decide", session, kwargs))
        if self.error:
            raise self.error
        return approval(state="APPROVED")
def approval(state="PENDING_HUMAN_APPROVAL"):
    return ClientApprovalView(
        approval_id="apr_test",
        state=state,
        capability="conversation.reply",
        operation="conversation.reply",
        target="synthetic-target",
        preview="Mensagem proposta",
        issued_at=NOW,
        expires_at=NOW.replace(minute=50),
    )


@pytest.fixture
def client():
    service = FakeService()
    session = FakeSession()

    def get_session():
        yield session

    app = FastAPI()
    app.include_router(
        build_client_approval_router(
            get_session=get_session,
            service=service,
        )
    )
    return TestClient(app), service, session


def auth():
    return {"Authorization": "Bearer " + "cst_" + "x" * 43}


def test_list_detail_and_decision_wire_shapes(client):
    http, service, session = client

    listed = http.get(PENDING, headers=auth())
    assert listed.status_code == 200
    assert listed.json()["contract_version"] == "1"
    assert listed.json()["approvals"][0]["approval_id"] == "apr_test"

    detail = http.get(DETAIL, headers=auth())
    assert detail.status_code == 200
    assert detail.json()["preview"] == "Mensagem proposta"

    decided = http.post(
        DECISION,
        headers=auth(),
        json={"decision": "APPROVE"},
    )
    assert decided.status_code == 200
    assert decided.json()["state"] == "APPROVED"
    assert service.calls[-1][0] == "decide"
    assert service.calls[-1][1] is session
    assert service.calls[-1][2]["decision"] == "APPROVE"
def test_runtime_openapi_matches_frozen_client_approval_contract(client):
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    frozen = json.loads(
        (root / "contracts/client/v1/client-api.openapi.json").read_text(
            encoding="utf-8"
        )
    )
    runtime = client[0].app.openapi()
    for path, methods in (
        ("/api/v1/client/approvals/pending", ("get",)),
        ("/api/v1/client/approvals/{approval_id}", ("get",)),
        ("/api/v1/client/approvals/{approval_id}/decision", ("post",)),
    ):
        for method in methods:
            assert (
                runtime["paths"][path][method]["operationId"]
                == frozen["paths"][path][method]["operationId"]
            )
            assert (
                runtime["paths"][path][method]["security"]
                == frozen["paths"][path][method]["security"]
            )
    assert set(
        runtime["components"]["schemas"]["ClientApprovalView"]
        ["properties"]["state"]["enum"]
    ) == set(
        frozen["components"]["schemas"]["ClientApprovalView"]
        ["properties"]["state"]["enum"]
    )


def test_decision_rejects_client_selected_authority_fields(client):
    http, _, _ = client
    response = http.post(
        DECISION,
        headers=auth(),
        json={
            "decision": "APPROVE",
            "tenant_id": "attacker-tenant",
            "human_identity_id": "attacker",
            "device_id": "attacker-device",
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (
            ClientApprovalAuthorityRejected(),
            403,
            "CLIENT_APPROVAL_AUTHORITY_REJECTED",
        ),
        (
            ClientApprovalConflict(),
            409,
            "CLIENT_APPROVAL_CONFLICT",
        ),
    ],
)
def test_errors_are_bounded(client, error, status_code, code):
    http, service, _ = client
    service.error = error
    response = http.get(PENDING, headers=auth())
    assert response.status_code == status_code
    assert response.json() == {"code": code}
