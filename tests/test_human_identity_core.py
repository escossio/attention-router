from dataclasses import FrozenInstanceError, asdict
from datetime import UTC, datetime

import pytest

from attention_router.core.human_identity import (
    HumanAuthChallengeConsumed,
    HumanAuthChallengeExpired,
    HumanAuthChallengeNotFound,
    HumanAuthCredentialRejected,
    HumanAuthDisabled,
    HumanAuthNonceMismatch,
    HumanAuthProviderUnavailable,
    HumanAuthState,
    HumanIdentityValidated,
    IssuedHumanAuthChallenge,
    VerifiedProviderIdentity,
)


NOW = datetime(2026, 9, 13, tzinfo=UTC)
EXPIRES = datetime(2026, 9, 13, 0, 5, tzinfo=UTC)


def test_human_auth_state_is_bounded():
    assert set(HumanAuthState.__members__) == {"PENDING", "VERIFIED", "REJECTED"}
    assert {state.value for state in HumanAuthState} == {"PENDING", "VERIFIED", "REJECTED"}
    assert all(isinstance(state, str) for state in HumanAuthState)
    with pytest.raises(ValueError):
        HumanAuthState("UNKNOWN")


def test_verified_provider_identity_is_provider_neutral_and_immutable():
    verified = VerifiedProviderIdentity(
        provider="synthetic-provider", subject="synthetic-subject", nonce="synthetic-nonce",
        issued_at=NOW, expires_at=EXPIRES,
    )
    assert asdict(verified) == {
        "provider": "synthetic-provider", "subject": "synthetic-subject",
        "nonce": "synthetic-nonce", "issued_at": NOW, "expires_at": EXPIRES,
    }
    for field in asdict(verified):
        with pytest.raises(FrozenInstanceError):
            setattr(verified, field, None)


def test_issued_challenge_has_only_immutable_challenge_data():
    challenge = IssuedHumanAuthChallenge(
        challenge_id="hac_abcdefghijklmnopqrst", nonce="synthetic-nonce", expires_at=EXPIRES,
    )
    assert asdict(challenge) == {
        "challenge_id": "hac_abcdefghijklmnopqrst", "nonce": "synthetic-nonce",
        "expires_at": EXPIRES,
    }
    for field in asdict(challenge):
        with pytest.raises(FrozenInstanceError):
            setattr(challenge, field, None)


def test_validated_result_exposes_only_immutable_opaque_identity_reference():
    result = HumanIdentityValidated(human_identity_id="hid_abcdefghijklmnopqrst")
    assert asdict(result) == {
        "human_identity_id": "hid_abcdefghijklmnopqrst", "status": "HUMAN_IDENTITY_VALIDATED",
    }
    for field in ("subject", "email", "tenant_id", "device_id", "session_id"):
        assert not hasattr(result, field)
    for field in asdict(result):
        with pytest.raises(FrozenInstanceError):
            setattr(result, field, None)


@pytest.mark.parametrize(
    ("exception_type", "code"),
    [
        (HumanAuthDisabled, "HUMAN_AUTH_DISABLED"),
        (HumanAuthChallengeNotFound, "HUMAN_AUTH_CHALLENGE_NOT_FOUND"),
        (HumanAuthChallengeExpired, "HUMAN_AUTH_CHALLENGE_EXPIRED"),
        (HumanAuthChallengeConsumed, "HUMAN_AUTH_CHALLENGE_CONSUMED"),
        (HumanAuthCredentialRejected, "HUMAN_AUTH_CREDENTIAL_REJECTED"),
        (HumanAuthNonceMismatch, "HUMAN_AUTH_NONCE_MISMATCH"),
        (HumanAuthProviderUnavailable, "HUMAN_AUTH_PROVIDER_UNAVAILABLE"),
    ],
)
def test_human_auth_exceptions_have_stable_semantic_codes(exception_type, code):
    with pytest.raises(exception_type) as caught:
        raise exception_type()
    assert caught.value.code == code
