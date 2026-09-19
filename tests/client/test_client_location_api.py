from contextlib import nullcontext
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from attention_router.api.v1.client_location import build_client_location_router
from attention_router.application.client_location import (
    ClientLocationAuthorityRejected,
    ClientLocationDisabled,
    ClientLocationFuture,
    ClientLocationNotFound,
    ClientLocationSnapshot,
    ClientLocationStale,
    ClientLocationUnauthenticated,
)
from attention_router.core.client.location import ClientLocationPrecision


CURRENT = "/api/v1/client/location/current"
TOKEN = "cst_" + "x" * 43


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


@pytest.fixture
def client():
    service, session = FakeService(), FakeSession()

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


def snapshot():
    return ClientLocationSnapshot(
        location_snapshot_id="cloc_" + "a" * 24,
        human_identity_id="hid_" + "b" * 24,
        device_id="cdev_" + "c" * 24,
        tenant_id="tnt_synthetic",
        latitude=-3.73,
        longitude=-38.54,
        accuracy_m=12.5,
        precision=ClientLocationPrecision.PRECISE,
        captured_at=datetime(2026, 9, 18, 23, 30, tzinfo=UTC),
        received_at=datetime(2026, 9, 18, 23, 30, 2, tzinfo=UTC),
    )


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_put_passes_observation_and_bearer_without_authority_claims(client):
    http, service, session = client
    response = http.put(
        CURRENT,
        headers=auth(),
        json={
            "latitude": -3.73,
            "longitude": -38.54,
            "accuracy_m": 12.5,
            "captured_at": "2026-09-18T23:30:00Z",
            "precision": "PRECISE",
        },
    )
    assert response.status_code == 200
    call = service.put_calls[-1]
    assert call[0] is session
    assert call[1]["session_token"] == TOKEN
    observation = call[1]["observation"]
    assert observation.latitude == -3.73
    assert observation.precision is ClientLocationPrecision.PRECISE
    assert "human_identity_id" not in call[1]
    assert "device_id" not in call[1]
    assert "tenant_id" not in call[1]


def test_get_passes_only_bearer_authority(client):
    http, service, session = client
    response = http.get(CURRENT, headers=auth())
    assert response.status_code == 200
    assert service.get_calls[-1] == (
        session,
        {"session_token": TOKEN},
    )
    assert response.json()["tenant_id"] == "tnt_synthetic"


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (ClientLocationStale(), 400, "CLIENT_LOCATION_STALE"),
        (ClientLocationFuture(), 400, "CLIENT_LOCATION_FUTURE"),
        (
            ClientLocationUnauthenticated(),
            401,
            "CLIENT_LOCATION_UNAUTHENTICATED",
        ),
        (
            ClientLocationAuthorityRejected(),
            403,
            "CLIENT_LOCATION_AUTHORITY_REJECTED",
        ),
        (ClientLocationNotFound(), 404, "CLIENT_LOCATION_NOT_FOUND"),
        (ClientLocationDisabled(), 503, "CLIENT_LOCATION_DISABLED"),
    ],
)
def test_errors_are_bounded(client, error, status_code, code):
    http, service, _ = client
    service.get_error = error
    response = http.get(CURRENT, headers=auth())
    assert response.status_code == status_code
    assert response.json() == {"code": code}


def test_missing_bearer_maps_to_bounded_unauthenticated(client):
    http, service, _ = client
    service.get_error = ClientLocationUnauthenticated()
    response = http.get(CURRENT)
    assert response.status_code == 401
    assert response.json() == {"code": "CLIENT_LOCATION_UNAUTHENTICATED"}
