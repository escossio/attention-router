"""Synthetic Google verification results; no provider network access."""

from dataclasses import asdict
from datetime import UTC
import socket

import pytest

from attention_router.core.human_identity import (
    HumanAuthCredentialRejected,
    HumanAuthProviderUnavailable,
    VerifiedProviderIdentity,
)
from attention_router.integrations.google_identity import GoogleIdentityVerifier


AUDIENCE = "server-client-id.example.apps.googleusercontent.com"
TOKEN = "synthetic-id-token"


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Google adapter tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.fixture
def claims():
    return {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "sub": "google-sub-123",
        "nonce": "nonce-value",
        "iat": 1789257600,
        "exp": 1789257900,
    }


@pytest.fixture
def google_verification(monkeypatch, claims):
    from google.oauth2 import id_token

    calls = []

    def verify(token, request, audience=None):
        calls.append((token, request, audience))
        return claims.copy()

    monkeypatch.setattr(id_token, "verify_oauth2_token", verify)
    return calls


@pytest.mark.parametrize("issuer", ["accounts.google.com", "https://accounts.google.com"])
def test_valid_token_returns_provider_identity_and_forwards_audience(
    claims, google_verification, issuer,
):
    from google.auth.transport.requests import Request

    claims["iss"] = issuer
    result = GoogleIdentityVerifier(audience=AUDIENCE).verify(TOKEN)

    assert isinstance(result, VerifiedProviderIdentity)
    assert result.provider == "google"
    assert result.subject == "google-sub-123"
    assert result.nonce == "nonce-value"
    assert result.issued_at.tzinfo is UTC
    assert result.expires_at.tzinfo is UTC
    assert result.issued_at.timestamp() == 1789257600
    assert result.expires_at.timestamp() == 1789257900
    assert len(google_verification) == 1
    token, request, audience = google_verification[0]
    assert token == TOKEN
    assert isinstance(request, Request)
    assert audience == AUDIENCE


@pytest.mark.parametrize("issuer", ["https://evil.example", "", None, []])
def test_other_issuers_are_rejected(claims, google_verification, issuer):
    claims["iss"] = issuer
    with pytest.raises(HumanAuthCredentialRejected) as error:
        GoogleIdentityVerifier(audience=AUDIENCE).verify(TOKEN)
    assert error.value.args == ()


@pytest.mark.parametrize("field", ["iss", "sub", "nonce", "iat", "exp"])
def test_missing_required_claim_is_rejected(claims, google_verification, field):
    del claims[field]
    with pytest.raises(HumanAuthCredentialRejected) as error:
        GoogleIdentityVerifier(audience=AUDIENCE).verify(TOKEN)
    assert error.value.args == ()


@pytest.mark.parametrize("field", ["sub", "nonce"])
@pytest.mark.parametrize("value", ["", None, 123, True, [], {}])
def test_subject_and_nonce_require_nonempty_strings(claims, google_verification, field, value):
    claims[field] = value
    with pytest.raises(HumanAuthCredentialRejected) as error:
        GoogleIdentityVerifier(audience=AUDIENCE).verify(TOKEN)
    assert error.value.args == ()


@pytest.mark.parametrize("field", ["iat", "exp"])
@pytest.mark.parametrize("value", [None, True, False, "1789257600", 1789257600.5, [], {}])
def test_timestamps_require_integers_excluding_bool(claims, google_verification, field, value):
    claims[field] = value
    with pytest.raises(HumanAuthCredentialRejected) as error:
        GoogleIdentityVerifier(audience=AUDIENCE).verify(TOKEN)
    assert error.value.args == ()


@pytest.mark.parametrize("field", ["iat", "exp"])
@pytest.mark.parametrize("value", [10**100, -(10**100)])
def test_unrepresentable_timestamps_are_rejected(claims, google_verification, field, value):
    claims[field] = value
    with pytest.raises(HumanAuthCredentialRejected) as error:
        GoogleIdentityVerifier(audience=AUDIENCE).verify(TOKEN)
    assert error.value.args == ()


@pytest.mark.parametrize("failure", ["credential", "transport"])
def test_google_errors_map_to_typed_errors_without_interpolated_messages(
    monkeypatch, claims, failure,
):
    from google.auth.exceptions import TransportError
    from google.oauth2 import id_token

    original = (ValueError if failure == "credential" else TransportError)(
        f"{TOKEN} {claims['nonce']} {claims['sub']} {claims}"
    )

    def fail_verification(token, request, audience=None):
        raise original

    monkeypatch.setattr(id_token, "verify_oauth2_token", fail_verification)
    expected = HumanAuthCredentialRejected if failure == "credential" else HumanAuthProviderUnavailable
    with pytest.raises(expected) as error:
        GoogleIdentityVerifier(audience=AUDIENCE).verify(TOKEN)
    assert error.value.__cause__ is original
    assert error.value.args == ()
    assert str(error.value) == ""


def test_email_is_not_returned_or_used_as_subject(claims, google_verification):
    claims["email"] = "synthetic@example.invalid"
    result = GoogleIdentityVerifier(audience=AUDIENCE).verify(TOKEN)
    assert result.subject == claims["sub"]
    assert set(asdict(result)) == {"provider", "subject", "nonce", "issued_at", "expires_at"}
    assert "email" not in asdict(result)
