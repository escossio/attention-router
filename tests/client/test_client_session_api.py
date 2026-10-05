from contextlib import nullcontext
from datetime import UTC, datetime
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from attention_router.api.v1.client_session import build_client_session_router
from attention_router.application.client_session import (
    AuthenticatedClientBootstrapResult, AuthenticatedClientTenantDirectoryResult,
    ClientSessionActiveTenantRequired, ClientTenantDirectoryMembership,
    ClientSessionAuthorityRejected, ClientSessionChallengeConsumed,
    ClientSessionDeviceRejected, ClientSessionUnauthenticated,
)
from attention_router.core.client.bootstrap import (
    ClientDevicePlatform, ClientDeviceRole, ClientDeviceStatus, ClientDeviceView,
    MembershipStatus, TenantMembershipView, TenantRole,
)
from attention_router.core.client.session import ClientSessionChallenge, ClientSessionIssued

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "contracts/client/v1/client-api.openapi.json").read_text(encoding="utf-8"))
START = "/api/v1/session/device/challenges"
COMPLETE = START + "/{session_challenge_id}/complete"
SNAPSHOT = "/api/v1/client/bootstrap"
TENANTS = "/api/v1/client/tenants"


class FakeSession:
    def begin_nested(self):
        return nullcontext()


class FakeService:
    def __init__(self):
        self.start_error = self.complete_error = self.bootstrap_error = None
        self.directory_error = None
        self.start_calls, self.complete_calls, self.bootstrap_calls = [], [], []
        self.directory_calls = []

    def start_session(self, session, **kwargs):
        self.start_calls.append((session, kwargs))
        if self.start_error:
            raise self.start_error
        return ClientSessionChallenge(
            session_challenge_id="csc_" + "a" * 24, challenge_b64url="b" * 43,
            expires_at=datetime(2026, 9, 18, 17, 0, tzinfo=UTC),
        )

    def complete_session(self, session, **kwargs):
        self.complete_calls.append((session, kwargs))
        if self.complete_error:
            raise self.complete_error
        return ClientSessionIssued(
            session_id="csn_" + "s" * 24, session_token="cst_" + "t" * 43,
            expires_at=datetime(2026, 9, 18, 17, 15, tzinfo=UTC),
            human_identity_id="hid_" + "h" * 24, device_id="cdev_" + "d" * 24,
            tenant_id="tnt_synthetic",
        )

    def authenticated_bootstrap(self, session, **kwargs):
        self.bootstrap_calls.append((session, kwargs))
        if self.bootstrap_error:
            raise self.bootstrap_error
        return AuthenticatedClientBootstrapResult(
            human_identity_id="hid_" + "h" * 24, active_tenant_id="tnt_synthetic",
            memberships=(TenantMembershipView(
                membership_id="ctm_synthetic", tenant_id="tnt_synthetic",
                role=TenantRole.OWNER, status=MembershipStatus.ACTIVE,
            ),),
            device=ClientDeviceView(
                device_id="cdev_" + "d" * 24, public_key_fingerprint="sha256:" + "f" * 64,
                canonical_name="Synthetic Android", platform=ClientDevicePlatform.ANDROID,
                roles=(ClientDeviceRole.CLIENT, ClientDeviceRole.CAPABILITY_NODE),
                status=ClientDeviceStatus.ACTIVE,
            ),
            session_expires_at=datetime(2026, 9, 18, 17, 15, tzinfo=UTC),
            server_time=datetime(2026, 9, 18, 17, 1, tzinfo=UTC),
        )


    def authenticated_tenant_directory(self, session, **kwargs):
        self.directory_calls.append((session, kwargs))
        if self.directory_error:
            raise self.directory_error
        return AuthenticatedClientTenantDirectoryResult(
            active_tenant_id="tnt_synthetic",
            memberships=(
                ClientTenantDirectoryMembership(
                    membership_id="ctm_synthetic",
                    tenant_id="tnt_synthetic",
                    display_name="Personal",
                    role=TenantRole.OWNER,
                    status=MembershipStatus.ACTIVE,
                ),
            ),
        )


@pytest.fixture
def client():
    service, session = FakeService(), FakeSession()
    def get_session():
        yield session
    app = FastAPI()
    app.include_router(build_client_session_router(get_session=get_session, service=service))
    return TestClient(app), service, session


def test_start_and_complete_wire_shapes(client):
    http, service, session = client
    start = http.post(START, json={"public_key_spki_b64url": "A" * 120})
    assert start.status_code == 201
    assert start.json()["session_challenge_id"].startswith("csc_")
    assert service.start_calls[0][0] is session
    complete = http.post(
        "/api/v1/session/device/challenges/" + "csc_" + "a" * 24 + "/complete",
        json={"device_signature_b64url": "c" * 96},
    )
    assert complete.status_code == 200
    assert complete.json()["session"]["session_token"] == "cst_" + "t" * 43
    assert "refresh_token" not in complete.text


@pytest.mark.parametrize(("error", "status_code", "code"), [
    (ClientSessionDeviceRejected(), 401, "CLIENT_SESSION_DEVICE_REJECTED"),
    (ClientSessionActiveTenantRequired(), 409, "CLIENT_SESSION_ACTIVE_TENANT_REQUIRED"),
    (ClientSessionChallengeConsumed(), 409, "CLIENT_SESSION_CHALLENGE_CONSUMED"),
])
def test_pre_session_errors_are_bounded(client, error, status_code, code):
    http, service, _ = client
    service.start_error = error
    response = http.post(START, json={"public_key_spki_b64url": "A" * 120})
    assert response.status_code == status_code
    assert response.json() == {"code": code}


def test_authenticated_bootstrap_requires_bearer(client):
    http, service, _ = client
    service.bootstrap_error = ClientSessionUnauthenticated()
    assert http.get(SNAPSHOT).status_code == 401
    service.bootstrap_error = None
    token = "cst_" + "x" * 43
    response = http.get(SNAPSHOT, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert service.bootstrap_calls[-1][1]["session_token"] == token
    assert "session_token" not in response.text


def test_authenticated_tenant_directory_is_additive_and_bounded(client):
    http, service, session = client
    token = "cst_" + "x" * 43
    response = http.get(
        TENANTS,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "contract_version": "1",
        "active_tenant_id": "tnt_synthetic",
        "memberships": [
            {
                "membership_id": "ctm_synthetic",
                "tenant_id": "tnt_synthetic",
                "display_name": "Personal",
                "role": "OWNER",
                "status": "ACTIVE",
            }
        ],
    }
    assert service.directory_calls[-1][0] is session
    assert service.directory_calls[-1][1]["session_token"] == token


def test_authenticated_tenant_directory_uses_session_errors(client):
    http, service, _ = client
    service.directory_error = ClientSessionAuthorityRejected()
    response = http.get(
        TENANTS,
        headers={"Authorization": "Bearer " + "cst_" + "x" * 43},
    )
    assert response.status_code == 403
    assert response.json() == {"code": "CLIENT_SESSION_AUTHORITY_REJECTED"}


def test_authority_rejection_maps_to_forbidden(client):
    http, service, _ = client
    service.bootstrap_error = ClientSessionAuthorityRejected()
    response = http.get(SNAPSHOT, headers={"Authorization": "Bearer " + "cst_" + "x" * 43})
    assert response.status_code == 403


def test_runtime_openapi_matches_frozen_v03c_contract(client):
    runtime = client[0].app.openapi()
    assert set(runtime["paths"]) == {START, COMPLETE, SNAPSHOT, TENANTS}
    for path, method in (
        (START, "post"),
        (COMPLETE, "post"),
        (SNAPSHOT, "get"),
        (TENANTS, "get"),
    ):
        actual, expected = runtime["paths"][path][method], CONTRACT["paths"][path][method]
        assert actual["operationId"] == expected["operationId"]
        assert actual["security"] == expected["security"]
        for status_code, response in expected["responses"].items():
            assert actual["responses"][status_code]["content"] == response["content"]
    scheme = runtime["components"]["securitySchemes"]["ClientSession"]
    assert scheme["scheme"] == "bearer"
    assert scheme["bearerFormat"] == "opaque-client-session-v03c"
