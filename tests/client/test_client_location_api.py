from contextlib import nullcontext
from datetime import UTC, datetime
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from attention_router.api.v1.client_location import build_client_location_router
from attention_router.application.message_location_context import MessageLocationContext
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
        self.message_calls = []
        self.message_error = None
        self.context_calls = []

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


    def put_context_observation(self, session, **kwargs):
        self.context_calls.append((session,kwargs))
        if self.put_error:
            raise self.put_error
        return snapshot()

    def get_message_context(self, session, **kwargs):
        self.message_calls.append((session, kwargs))
        if self.message_error:
            raise self.message_error
        return MessageLocationContext(
            message_id=kwargs["message_id"],
            status="NO_TIME_MATCHED_LOCATION",
            evidence_basis="ANDROID_DEVICE_POSITION_NEAR_WHATSAPP_MESSAGE_TIME",
            message_at=NOW,
        )


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

def test_owner_message_location_endpoint_returns_explicit_no_evidence(client):
    http, service, session = client
    token = "cst_" + "x"*43
    result = http.get(
        "/api/v1/client/location/message/cmsg_synthetic",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert result.status_code == 200
    body = result.json()
    assert body["contract_version"] == "1"
    assert body["message_id"] == "cmsg_synthetic"
    assert body["status"] == "NO_TIME_MATCHED_LOCATION"
    assert body["evidence_basis"] == "ANDROID_DEVICE_POSITION_NEAR_WHATSAPP_MESSAGE_TIME"
    assert "latitude" not in body and "longitude" not in body
    assert service.message_calls[0][0] is session
    assert service.message_calls[0][1]["session_token"] == token


def test_message_location_endpoint_rejects_missing_or_other_tenant(client):
    from attention_router.application.client_location import ClientLocationAuthorityRejected
    http, service, _ = client
    service.message_error = ClientLocationAuthorityRejected()
    denial = http.get(
        "/api/v1/client/location/message/cmsg_synthetic",
        headers={"Authorization": "Bearer cst_"+"x"*43},
    )
    assert denial.status_code == 403
    assert denial.json() == {"code": "CLIENT_LOCATION_AUTHORITY_REJECTED"}
    invalid = http.get(
        "/api/v1/client/location/message/"+"y"*80,
        headers={"Authorization": "Bearer cst_"+"x"*43},
    )
    assert invalid.status_code == 404


def test_message_location_endpoint_returns_evidence_not_human_presence_claim(client):
    http, service, _ = client
    def result(session, **kwargs):
        return MessageLocationContext(
            message_id=kwargs["message_id"],
            status="DEVICE_POSITION_NEAR_MESSAGE",
            evidence_basis="ANDROID_DEVICE_POSITION_NEAR_WHATSAPP_MESSAGE_TIME",
            message_at=NOW,
            location_observation_id="clh_synthetic",
            location_device_id="cdev_synthetic",
            latitude=-3.7,
            longitude=-38.5,
            accuracy_m=12.5,
            precision="PRECISE",
            captured_at=NOW,
            delta_seconds=0.0,
        )
    service.get_message_context = result
    response = http.get(
        "/api/v1/client/location/message/cmsg_synthetic",
        headers={"Authorization": "Bearer cst_"+"x"*43},
    )
    assert response.status_code == 200
    assert response.json()["status"]=="DEVICE_POSITION_NEAR_MESSAGE"
    assert response.json()["accuracy_m"]==12.5
    assert response.json()["evidence_basis"].startswith("ANDROID_DEVICE_")

def test_history_observation_requires_explicit_optin_and_does_not_change_current(client):
    http, service, sess = client
    headers={"Authorization":"Bearer cst_"+"x"*43}
    base={
        "latitude":-3.73,"longitude":-38.54,"accuracy_m":12.5,
        "captured_at":"2026-09-18T23:30:00Z","precision":"PRECISE",
    }
    path="/api/v1/client/location/context/observations"
    missing=http.put(path,headers=headers,json=base)
    assert missing.status_code==422
    denied=http.put(path,headers=headers,json={**base,"context_history_opt_in":False})
    assert denied.status_code==422
    yes=http.put(path,headers=headers,json={**base,"context_history_opt_in":True})
    assert yes.status_code==200
    assert yes.json()["location_snapshot_id"].startswith("cloc_")
    assert len(service.context_calls)==1
    assert service.context_calls[0][0] is sess
    assert service.context_calls[0][1]["session_token"]==headers["Authorization"].split(" ")[1]
    old=http.put("/api/v1/client/location/current",headers=headers,json=base)
    assert old.status_code==200
    assert len(service.put_calls)==1
    assert len(service.context_calls)==1
