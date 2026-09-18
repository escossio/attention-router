from contextlib import nullcontext
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from attention_router.api.v1.client_bootstrap import build_client_bootstrap_router
from attention_router.application.client_bootstrap import (
    DeviceBootstrapChallengeConsumed,
    DeviceBootstrapChallengeExpired,
    DeviceBootstrapChallengeNotFound,
    DeviceBootstrapDeviceConflict,
    DeviceBootstrapDeviceKeyInvalid,
    DeviceBootstrapDisabled,
    DeviceBootstrapGrantRejected,
    DeviceBootstrapMembershipConflict,
    DeviceBootstrapSignatureInvalid,
    DeviceBootstrapUnavailable,
)
from attention_router.core.client.bootstrap import (
    ClientDevicePlatform,
    ClientDeviceRole,
    ClientDeviceStatus,
    ClientDeviceView,
    DeviceBootstrapChallenge,
    DeviceBootstrapEstablished,
    MembershipStatus,
    TenantMembershipView,
    TenantRole,
)


class FakeSession:
    def begin_nested(self):
        return nullcontext()


class FakeService:
    def __init__(self):
        self.start_error = None
        self.complete_error = None
        self.start_calls = []
        self.complete_calls = []

    def start_device_bootstrap(self, session, **kwargs):
        self.start_calls.append((session, kwargs))
        if self.start_error:
            raise self.start_error
        return DeviceBootstrapChallenge(
            bootstrap_challenge_id="dbc_" + "a" * 24,
            challenge_b64url="b" * 43,
            expires_at=datetime(2026, 9, 18, tzinfo=UTC),
        )

    def complete_device_bootstrap(self, session, **kwargs):
        self.complete_calls.append((session, kwargs))
        if self.complete_error:
            raise self.complete_error
        return DeviceBootstrapEstablished(
            human_identity_id="hid_" + "h" * 24,
            memberships=(
                TenantMembershipView(
                    membership_id="ctm_synthetic",
                    tenant_id="tnt_synthetic",
                    role=TenantRole.OWNER,
                    status=MembershipStatus.ACTIVE,
                ),
            ),
            initial_tenant_id="tnt_synthetic",
            device=ClientDeviceView(
                device_id="cdev_" + "d" * 24,
                public_key_fingerprint="sha256:" + "f" * 64,
                canonical_name="Synthetic Android",
                platform=ClientDevicePlatform.ANDROID,
                roles=(ClientDeviceRole.CLIENT, ClientDeviceRole.CAPABILITY_NODE),
                status=ClientDeviceStatus.ACTIVE,
            ),
        )


@pytest.fixture
def client():
    service = FakeService()
    session = FakeSession()

    def get_session():
        yield session

    app = FastAPI()
    app.include_router(build_client_bootstrap_router(
        get_session=get_session,
        service=service,
    ))
    return TestClient(app), service, session


def _start_payload():
    return {
        "continuation_token": "hcg_" + "a" * 43,
        "public_key_spki_b64url": "A" * 120,
        "canonical_device_name": "Synthetic Android",
        "platform": "ANDROID",
        "roles": ["CLIENT", "CAPABILITY_NODE"],
    }


def test_start_wire_shape_and_sensitive_grant_not_echoed(client):
    http, service, session = client
    response = http.post("/api/v1/bootstrap/device/challenges", json=_start_payload())

    assert response.status_code == 201
    assert response.json() == {
        "bootstrap_challenge_id": "dbc_" + "a" * 24,
        "challenge_b64url": "b" * 43,
        "expires_at": "2026-09-18T00:00:00Z",
    }
    assert service.start_calls[0][0] is session
    assert "continuation_token" not in response.text
    assert "tenant_id" not in response.text


def test_start_rejects_client_selected_tenant_before_service(client):
    http, service, _ = client
    payload = _start_payload() | {"tenant_id": "attacker-selected"}
    response = http.post("/api/v1/bootstrap/device/challenges", json=payload)

    assert response.status_code == 422
    assert service.start_calls == []


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (DeviceBootstrapDeviceKeyInvalid(), 400, "DEVICE_BOOTSTRAP_DEVICE_KEY_INVALID"),
        (DeviceBootstrapGrantRejected(), 401, "DEVICE_BOOTSTRAP_GRANT_REJECTED"),
        (DeviceBootstrapChallengeConsumed(), 409, "DEVICE_BOOTSTRAP_CHALLENGE_CONSUMED"),
        (DeviceBootstrapDisabled(), 503, "DEVICE_BOOTSTRAP_UNAVAILABLE"),
    ],
)
def test_start_maps_bounded_errors(client, error, status_code, code):
    http, service, _ = client
    service.start_error = error
    response = http.post("/api/v1/bootstrap/device/challenges", json=_start_payload())

    assert response.status_code == status_code
    assert response.json() == {"code": code}


def test_complete_returns_bounded_authority_without_session(client):
    http, _, _ = client
    response = http.post(
        "/api/v1/bootstrap/device/challenges/" + "dbc_" + "a" * 24 + "/complete",
        json={"device_signature_b64url": "c" * 96},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "DEVICE_BOOTSTRAP_ESTABLISHED"
    assert payload["human_identity_id"].startswith("hid_")
    assert payload["initial_tenant_id"] == "tnt_synthetic"
    assert payload["memberships"][0]["role"] == "OWNER"
    assert payload["device"]["status"] == "ACTIVE"
    raw = response.text
    for forbidden in ("access_token", "refresh_token", "session_id", "continuation_token"):
        assert forbidden not in raw


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (DeviceBootstrapGrantRejected(), 401, "DEVICE_BOOTSTRAP_GRANT_REJECTED"),
        (DeviceBootstrapChallengeNotFound(), 404, "DEVICE_BOOTSTRAP_CHALLENGE_NOT_FOUND"),
        (DeviceBootstrapChallengeExpired(), 410, "DEVICE_BOOTSTRAP_CHALLENGE_EXPIRED"),
        (DeviceBootstrapChallengeConsumed(), 409, "DEVICE_BOOTSTRAP_CHALLENGE_CONSUMED"),
        (DeviceBootstrapSignatureInvalid(), 401, "DEVICE_BOOTSTRAP_SIGNATURE_INVALID"),
        (DeviceBootstrapMembershipConflict(), 409, "DEVICE_BOOTSTRAP_MEMBERSHIP_CONFLICT"),
        (DeviceBootstrapDeviceConflict(), 409, "DEVICE_BOOTSTRAP_DEVICE_CONFLICT"),
        (DeviceBootstrapUnavailable(), 503, "DEVICE_BOOTSTRAP_UNAVAILABLE"),
    ],
)
def test_complete_maps_bounded_errors(client, error, status_code, code):
    http, service, _ = client
    service.complete_error = error
    response = http.post(
        "/api/v1/bootstrap/device/challenges/" + "dbc_" + "a" * 24 + "/complete",
        json={"device_signature_b64url": "c" * 96},
    )

    assert response.status_code == status_code
    assert response.json() == {"code": code}
