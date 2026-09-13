import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from attention_router.api.v1.human_identity import build_human_identity_router
from attention_router.core.human_identity import (
    HumanAuthChallengeConsumed,
    HumanAuthChallengeExpired,
    HumanAuthChallengeNotFound,
    HumanAuthCredentialRejected,
    HumanAuthDisabled,
    HumanAuthNonceMismatch,
    HumanAuthProviderUnavailable,
    HumanIdentityValidated,
    IssuedHumanAuthChallenge,
)


CHALLENGES = "/api/v1/auth/google/challenges"
VERIFY = CHALLENGES + "/{challenge_id}/verify"
CHALLENGE_ID = "hac_examplechallenge123456789"
ID_TOKEN = "synthetic-token-value-that-is-at-least-32-characters"
ERRORS = [
    (HumanAuthCredentialRejected, 401, "HUMAN_AUTH_CREDENTIAL_REJECTED"),
    (HumanAuthNonceMismatch, 401, "HUMAN_AUTH_NONCE_MISMATCH"),
    (HumanAuthChallengeNotFound, 404, "HUMAN_AUTH_CHALLENGE_NOT_FOUND"),
    (HumanAuthChallengeConsumed, 409, "HUMAN_AUTH_CHALLENGE_CONSUMED"),
    (HumanAuthChallengeExpired, 410, "HUMAN_AUTH_CHALLENGE_EXPIRED"),
    (HumanAuthProviderUnavailable, 503, "HUMAN_AUTH_PROVIDER_UNAVAILABLE"),
    (HumanAuthDisabled, 503, "HUMAN_AUTH_DISABLED"),
]
CONTRACT = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts/client/v1/client-api.openapi.json")
    .read_text(encoding="utf-8")
)


class FakeService:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def issue_google_challenge(self, session):
        self.calls.append((session,))
        if self.error:
            raise self.error()
        return IssuedHumanAuthChallenge(
            challenge_id=CHALLENGE_ID,
            nonce="n" * 32,
            expires_at=datetime(2026, 9, 13, 12, 5, tzinfo=UTC),
        )

    def verify_google_challenge(self, session, challenge_id, id_token):
        self.calls.append((session, challenge_id, id_token))
        if self.error:
            raise self.error()
        return HumanIdentityValidated(human_identity_id="hid_exampleopaqueidentity123")


def isolated_app(service, get_session):
    app = FastAPI()
    app.include_router(build_human_identity_router(get_session=get_session, service=service))
    return app


@pytest.fixture
def api():
    session = object()
    service = FakeService()

    def get_session():
        yield session

    return TestClient(isolated_app(service, get_session)), service, session


def test_challenge_success_without_admin_authorization(api):
    client, service, session = api
    response = client.post(CHALLENGES)
    assert response.status_code == 201
    assert set(response.json()) == {"challenge_id", "nonce", "expires_at"}
    assert response.json() == {
        "challenge_id": CHALLENGE_ID,
        "nonce": "n" * 32,
        "expires_at": "2026-09-13T12:05:00Z",
    }
    assert service.calls == [(session,)]


def test_verify_success_is_private_and_passes_exact_arguments_without_admin(api):
    client, service, session = api
    response = client.post(VERIFY.format(challenge_id=CHALLENGE_ID), json={"id_token": ID_TOKEN})
    assert response.status_code == 200
    assert response.json() == {
        "status": "HUMAN_IDENTITY_VALIDATED",
        "human_identity_id": "hid_exampleopaqueidentity123",
    }
    assert set(response.json()) == {"status", "human_identity_id"}
    assert service.calls == [(session, CHALLENGE_ID, ID_TOKEN)]


@pytest.mark.parametrize("error,status,code", ERRORS)
def test_domain_error_mapping_matches_contract(api, error, status, code):
    client, service, _ = api
    service.error = error
    response = client.post(VERIFY.format(challenge_id=CHALLENGE_ID), json={"id_token": ID_TOKEN})
    assert response.status_code == status
    assert response.json() == {"code": code}
    assert set(response.json()) == {"code"}
    assert str(status) in CONTRACT["paths"][VERIFY]["post"]["responses"]
    assert code in CONTRACT["components"]["schemas"]["HumanAuthErrorResponse"]["properties"]["code"]["enum"]


def test_disabled_challenge_has_code_only(api):
    client, service, _ = api
    service.error = HumanAuthDisabled
    response = client.post(CHALLENGES)
    assert response.status_code == 503
    assert response.json() == {"code": "HUMAN_AUTH_DISABLED"}


@pytest.mark.parametrize("body", [
    {"id_token": ID_TOKEN, "tenant_id": "forbidden"},
    {},
    {"id_token": "x" * 31},
    {"id_token": "x" * 8193},
], ids=["extra-property", "missing-token", "short-token", "long-token"])
def test_invalid_request_never_calls_service(api, body):
    client, service, _ = api
    response = client.post(VERIFY.format(challenge_id=CHALLENGE_ID), json=body)
    assert response.status_code == 422
    assert service.calls == []


@pytest.mark.parametrize("challenge_id", ["invalid", "hac_" + "a" * 19, "hac_" + "!" * 20])
def test_invalid_challenge_id_never_calls_service(api, challenge_id):
    client, service, _ = api
    response = client.post(VERIFY.format(challenge_id=challenge_id), json={"id_token": ID_TOKEN})
    assert response.status_code == 422
    assert service.calls == []


@pytest.mark.parametrize("length", [32, 8192])
def test_token_length_boundaries_are_accepted(api, length):
    client, service, session = api
    token = "x" * length
    challenge_id = "hac_" + "Aa0_-" * 4
    response = client.post(VERIFY.format(challenge_id=challenge_id), json={"id_token": token})
    assert response.status_code == 200
    assert service.calls == [(session, challenge_id, token)]


def test_runtime_openapi_matches_frozen_contract(api):
    client, _, _ = api
    runtime = client.app.openapi()
    assert set(runtime["paths"]) == set(CONTRACT["paths"])
    for path, item in CONTRACT["paths"].items():
        operation = runtime["paths"][path]["post"]
        assert operation.get("security", []) == item["post"]["security"] == []
        assert not any(p["in"] == "header" for p in operation.get("parameters", []))
        for status, response in item["post"]["responses"].items():
            assert operation["responses"][status]["content"] == response["content"]
    expected_parameter = CONTRACT["paths"][VERIFY]["post"]["parameters"][0]
    parameter = runtime["paths"][VERIFY]["post"]["parameters"][0]
    assert parameter["schema"]["pattern"] == expected_parameter["schema"]["pattern"]
    for name, expected in CONTRACT["components"]["schemas"].items():
        actual = runtime["components"]["schemas"][name]
        assert actual["additionalProperties"] is False
        assert set(actual["required"]) == set(expected["required"])
        assert set(actual["properties"]) == set(expected["properties"])
        for field, constraints in expected["properties"].items():
            for key, value in constraints.items():
                assert actual["properties"][field][key] == value


@pytest.mark.parametrize("error,code", [
    (HumanAuthCredentialRejected, "HUMAN_AUTH_CREDENTIAL_REJECTED"),
    (HumanAuthNonceMismatch, "HUMAN_AUTH_NONCE_MISMATCH"),
])
def test_deterministic_rejection_reaches_dependency_commit_path(error, code):
    events = []

    def get_session():
        try:
            yield object()
            events.append("DEPENDENCY_COMMIT_PATH")
        except Exception:
            events.append("DEPENDENCY_ROLLBACK_PATH")
            raise

    client = TestClient(isolated_app(FakeService(error), get_session))
    response = client.post(VERIFY.format(challenge_id=CHALLENGE_ID), json={"id_token": ID_TOKEN})
    assert response.status_code == 401
    assert response.json() == {"code": code}
    assert events == ["DEPENDENCY_COMMIT_PATH"]


@pytest.mark.parametrize("path,body", [
    (CHALLENGES, None),
    (VERIFY.format(challenge_id=CHALLENGE_ID), {"id_token": ID_TOKEN}),
])
def test_real_app_disabled_without_admin_authorization(path, body):
    from attention_router.web.app import app, settings

    assert settings.human_identity_enabled is False
    assert not settings.google_identity_audience
    assert settings.admin_auth_enabled is True
    response = TestClient(app).post(path, json=body)
    assert response.status_code == 503
    assert response.json() == {"code": "HUMAN_AUTH_DISABLED"}
