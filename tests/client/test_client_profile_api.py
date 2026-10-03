from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from attention_router.api.v1.client_profile import build_client_profile_router
from attention_router.application.client_profile import (
    ClientProfileAuthorityRejected,
    ClientProfileView,
)


NOW = datetime(2026, 10, 3, 6, 0, tzinfo=UTC)
PROFILE = "/api/v1/client/profile"


class FakeService:
    def __init__(self):
        self.error = None
        self.calls = []

    def get(self, session, **kwargs):
        self.calls.append(("get", session, kwargs))
        if self.error:
            raise self.error
        return ClientProfileView("Leonardo", NOW)

    def update(self, session, **kwargs):
        self.calls.append(("update", session, kwargs))
        if self.error:
            raise self.error
        return ClientProfileView(kwargs["assistant_reference_name"], NOW)


@pytest.fixture
def client():
    service = FakeService()
    session = object()

    def get_session():
        yield session

    app = FastAPI()
    app.include_router(build_client_profile_router(get_session=get_session, service=service))
    return TestClient(app), service, session


def auth():
    return {"Authorization": "Bearer " + "cst_" + "x" * 43}


def test_profile_get_and_patch_wire_shape(client):
    http, service, session = client
    fetched = http.get(PROFILE, headers=auth())
    assert fetched.status_code == 200
    assert fetched.json()["assistant_reference_name"] == "Leonardo"
    changed = http.patch(PROFILE, headers=auth(), json={"assistant_reference_name": "Leo"})
    assert changed.status_code == 200
    assert changed.json()["assistant_reference_name"] == "Leo"
    assert service.calls[-1][0] == "update"
    assert service.calls[-1][1] is session


def test_profile_patch_requires_explicit_field(client):
    assert client[0].patch(PROFILE, headers=auth(), json={}).status_code == 422


def test_profile_authority_error_is_bounded(client):
    http, service, _ = client
    service.error = ClientProfileAuthorityRejected()
    response = http.get(PROFILE, headers=auth())
    assert response.status_code == 403
    assert response.json() == {"code": "CLIENT_PROFILE_AUTHORITY_REJECTED"}
