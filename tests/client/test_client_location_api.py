from contextlib import nullcontext
from datetime import UTC, datetime
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from attention_router.api.v1.client_location import build_client_location_router
from attention_router.application.client_location import (
    ClientLocationAuthorityRejected,
    ClientLocationSnapshotResult,
    ClientLocationStale,
    ClientLocationUnauthenticated,
)


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads(
    (ROOT / "contracts/client/v1/client-api.openapi.json").read_text(
        encoding="utf-8"
    )
)
CURRENT = "/api/v1/client/location/current"
NOW = datetime(2026, 9, 18, 23, 30, tzinfo=UTC)


class FakeSession:
    def begin_nested(self):
        return nullcontext()


class FakeService:
    def __init__(self):
        self.put_error = None
        self.get_error = None
        self.put_calls = []
        self.get_calls = []

    def put_current(self, session, **kwargs):
        self.put_calls.append((session, kwargs))
        if self.put_error:
            raise self.put_error
        return snapshot()

    def get_current(self, session, **kwargs):
        self.get_calls.append((session, kwargs))
        if self.get_error:
            raise self.get_error
        return snapshot()


def snapshot():
    return ClientLocationSnapshotResult(
        location_snapshot_id="cloc_" + "a" * 24,
        human_identity_id="hid_" + "h" * 24,
        device_id="cdev_" + "d" * 24,
        tenant_id="tnt_synthetic",
        latitude=-3.73,
        longitude=-38.54,
        accuracy_m=12.5,
        precision="PRECISE",
        captured_at=NOW,
        received_at=NOW,
    )


@pytest.fixture
def client():
    service = FakeService()
    session = FakeSession()

    def get_session():
        yield session

    app = FastAPI()
    app.include_router(
        build_client_location_router(
            get_session=get_session,
            service=service,
        )
    )
    return TestClient(app), service, session


def test_authenticated_put_and_get_wire_shapes(client):
    http, service, session = client
    token = "cst_" + "x" * 43
    headers = {"Authorization": f"Bearer {token}"}

    put = http.put(
        CURRENT,
        headers=headers,
        json={
            "latitude": -3.73,
            "longitude": -38.54,
            "accuracy_m": 12.5,
            "captured_at": "2026-09-18T23:30:00Z",
            "precision": "PRECISE",
        },
    )
    assert put.status_code == 200
    assert put.json()["tenant_id"] == "tnt_synthetic"
    assert service.put_calls[0][0] is session
    assert service.put_calls[0][1]["session_token"] == token
    assert service.put_calls[0][1]["observation"].latitude == -3.73

    get = http.get(CURRENT, headers=headers)
    assert get.status_code == 200
    assert get.json()["device_id"].startswith("cdev_")
    assert service.get_calls[0][1]["session_token"] == token


def test_missing_bearer_maps_to_unauthenticated(client):
    http, service, _ = client
    service.get_error = ClientLocationUnauthenticated()
    response = http.get(CURRENT)
    assert response.status_code == 401
    assert response.json() == {"code": "CLIENT_LOCATION_UNAUTHENTICATED"}


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (
            ClientLocationAuthorityRejected(),
            403,
            "CLIENT_LOCATION_AUTHORITY_REJECTED",
        ),
        (
            ClientLocationStale(),
            400,
            "CLIENT_LOCATION_STALE",
        ),
    ],
)
def test_location_errors_are_bounded(client, error, status_code, code):
    http, service, _ = client
    service.put_error = error
    response = http.put(
        CURRENT,
        headers={"Authorization": "Bearer " + "cst_" + "x" * 43},
        json={
            "latitude": -3.73,
            "longitude": -38.54,
            "accuracy_m": 12.5,
            "captured_at": "2026-09-18T23:30:00Z",
        },
    )
    assert response.status_code == status_code
    assert response.json() == {"code": code}


def test_request_rejects_client_selected_authority_ids(client):
    http, _, _ = client
    response = http.put(
        CURRENT,
        headers={"Authorization": "Bearer " + "cst_" + "x" * 43},
        json={
            "latitude": -3.73,
            "longitude": -38.54,
            "accuracy_m": 12.5,
            "captured_at": "2026-09-18T23:30:00Z",
            "tenant_id": "tnt_attacker",
        },
    )
    assert response.status_code == 422


def test_runtime_openapi_matches_frozen_v04a_contract(client):
    runtime = client[0].app.openapi()
    actual = runtime["paths"][CURRENT]
    expected = CONTRACT["paths"][CURRENT]
    for method in ("put", "get"):
        assert actual[method]["operationId"] == expected[method]["operationId"]
        assert actual[method]["security"] == expected[method]["security"]
        for status_code, response in expected[method]["responses"].items():
            assert actual[method]["responses"][status_code]["content"] == response["content"]
    scheme = runtime["components"]["securitySchemes"]["ClientSession"]
    assert scheme["scheme"] == "bearer"
    assert scheme["bearerFormat"] == "opaque-client-session-v03c"
