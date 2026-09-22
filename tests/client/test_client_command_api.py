from contextlib import nullcontext
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from attention_router.api.v1.client_command import build_client_command_router
from attention_router.application.client_command import (
    ClientCommandAuthorityRejected,
    ClientCommandConflict,
    ClientCommandView,
)


NOW = datetime(2026, 9, 22, 10, 30, tzinfo=UTC)
COMMANDS = "/api/v1/client/commands"


class FakeSession:
    def begin_nested(self):
        return nullcontext()


class FakeService:
    def __init__(self):
        self.error = None
        self.calls = []

    def submit_text(self, session, **kwargs):
        self.calls.append(("submit", session, kwargs))
        if self.error:
            raise self.error
        return command()

    def list_recent(self, session, **kwargs):
        self.calls.append(("list", session, kwargs))
        if self.error:
            raise self.error
        return (command(),)


def command():
    return ClientCommandView(
        command_id="cmd_test",
        client_request_id="req_test",
        modality="TEXT",
        input_text="pare",
        state="COMPLETED",
        normalized_action="SET_AUTOMATIC_RESPONSES_ENABLED",
        response_text="Andy pausada.",
        error_code=None,
        created_at=NOW,
        processed_at=NOW,
    )


@pytest.fixture
def client():
    service = FakeService()
    session = FakeSession()

    def get_session():
        yield session

    app = FastAPI()
    app.include_router(
        build_client_command_router(
            get_session=get_session,
            service=service,
        )
    )
    return TestClient(app), service, session


def auth():
    return {"Authorization": "Bearer " + "cst_" + "x" * 43}


def test_submit_and_list_wire_shapes(client):
    http, service, session = client

    submitted = http.post(
        COMMANDS,
        headers=auth(),
        json={"client_request_id": "req_test", "text": "pare"},
    )
    assert submitted.status_code == 200
    assert submitted.json()["state"] == "COMPLETED"
    assert submitted.json()["input_text"] == "pare"
    assert service.calls[-1][0] == "submit"
    assert service.calls[-1][1] is session
    assert service.calls[-1][2]["text"] == "pare"

    listed = http.get(COMMANDS, headers=auth())
    assert listed.status_code == 200
    assert listed.json()["contract_version"] == "1"
    assert listed.json()["commands"][0]["command_id"] == "cmd_test"


def test_runtime_openapi_matches_frozen_command_contract(client):
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    frozen = json.loads(
        (root / "contracts/client/v1/client-api.openapi.json").read_text(
            encoding="utf-8"
        )
    )
    runtime = client[0].app.openapi()
    for method in ("post", "get"):
        assert (
            runtime["paths"][COMMANDS][method]["operationId"]
            == frozen["paths"][COMMANDS][method]["operationId"]
        )
        assert (
            runtime["paths"][COMMANDS][method]["security"]
            == frozen["paths"][COMMANDS][method]["security"]
        )
    assert set(
        runtime["components"]["schemas"]["ClientCommandView"]
        ["properties"]["state"]["enum"]
    ) == set(
        frozen["components"]["schemas"]["ClientCommandView"]
        ["properties"]["state"]["enum"]
    )


def test_client_cannot_select_authority_fields(client):
    http, _, _ = client
    response = http.post(
        COMMANDS,
        headers=auth(),
        json={
            "client_request_id": "req_test",
            "text": "pare",
            "tenant_id": "attacker",
            "human_identity_id": "attacker",
            "device_id": "attacker",
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (
            ClientCommandAuthorityRejected(),
            403,
            "CLIENT_COMMAND_AUTHORITY_REJECTED",
        ),
        (
            ClientCommandConflict(),
            409,
            "CLIENT_COMMAND_CONFLICT",
        ),
    ],
)
def test_errors_are_bounded(client, error, status_code, code):
    http, service, _ = client
    service.error = error
    response = http.post(
        COMMANDS,
        headers=auth(),
        json={"client_request_id": "req_test", "text": "pare"},
    )
    assert response.status_code == status_code
    assert response.json() == {"code": code}
