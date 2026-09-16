from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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


class FakeHumanIdentityService:
    def __init__(self, error: Exception | None = None):
        self.error = error

    def issue_google_challenge(self, session):
        if self.error:
            raise self.error
        return IssuedHumanAuthChallenge(
            challenge_id="hac_examplechallenge123456789",
            nonce="synthetic-nonce-value-that-is-long-enough",
            expires_at=datetime(2026, 9, 13, tzinfo=UTC),
        )

    def verify_google_challenge(self, session, challenge_id, id_token):
        if self.error:
            raise self.error
        return HumanIdentityValidated(human_identity_id="hid_exampleopaqueidentity123")


def _client(service):
    from attention_router.api.v1.human_identity import build_human_identity_router

    def get_session():
        yield object()

    app = FastAPI()
    app.include_router(build_human_identity_router(get_session=get_session, service=service))
    return TestClient(app, raise_server_exceptions=False)


def test_issue_challenge_matches_contract_response():
    response = _client(FakeHumanIdentityService()).post("/api/v1/auth/google/challenges")
    assert response.status_code == 201
    assert set(response.json()) == {"challenge_id", "nonce", "expires_at"}


def test_verify_challenge_matches_contract_response():
    response = _client(FakeHumanIdentityService()).post(
        "/api/v1/auth/google/challenges/hac_examplechallenge123456789/verify",
        json={"id_token": "synthetic-token-value-that-is-not-logged"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "status": "HUMAN_IDENTITY_VALIDATED",
        "human_identity_id": "hid_exampleopaqueidentity123",
    }


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (HumanAuthChallengeNotFound(), 404, "HUMAN_AUTH_CHALLENGE_NOT_FOUND"),
        (HumanAuthChallengeExpired(), 410, "HUMAN_AUTH_CHALLENGE_EXPIRED"),
        (HumanAuthChallengeConsumed(), 409, "HUMAN_AUTH_CHALLENGE_CONSUMED"),
        (HumanAuthCredentialRejected(), 401, "HUMAN_AUTH_CREDENTIAL_REJECTED"),
        (HumanAuthNonceMismatch(), 401, "HUMAN_AUTH_NONCE_MISMATCH"),
        (HumanAuthProviderUnavailable(), 503, "HUMAN_AUTH_PROVIDER_UNAVAILABLE"),
        (HumanAuthDisabled(), 503, "HUMAN_AUTH_DISABLED"),
    ],
)
def test_human_identity_errors_are_stable_and_sensitive_data_free(error, status_code, code):
    response = _client(FakeHumanIdentityService(error)).post(
        "/api/v1/auth/google/challenges/hac_examplechallenge123456789/verify",
        json={"id_token": "synthetic-token-value-that-is-not-logged"},
    )
    assert response.status_code == status_code
    assert response.json() == {"code": code}


def test_extra_verify_json_properties_are_rejected():
    response = _client(FakeHumanIdentityService()).post(
        "/api/v1/auth/google/challenges/hac_examplechallenge123456789/verify",
        json={
            "id_token": "synthetic-token-value-that-is-not-logged",
            "unexpected": "not accepted",
        },
    )
    assert response.status_code == 422


def test_disabled_feature_keeps_challenge_route_registered_and_fail_closed():
    response = _client(FakeHumanIdentityService(HumanAuthDisabled())).post(
        "/api/v1/auth/google/challenges"
    )
    assert response.status_code == 503
    assert response.json() == {"code": "HUMAN_AUTH_DISABLED"}
