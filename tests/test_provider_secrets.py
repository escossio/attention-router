import base64

import pytest

from attention_router.security.provider_secrets import (
    ProviderSecretCipher,
)


def _key():
    return base64.urlsafe_b64encode(b"K" * 32).decode().rstrip("=")


def test_provider_secret_envelope_round_trips_without_plaintext():
    cipher = ProviderSecretCipher(_key())
    aad = b"installation|tenant|human|GOOGLE|GMAIL"

    envelope = cipher.encrypt(
        refresh_token="refresh-secret",
        integration_bearer="ingress-secret",
        aad=aad,
    )

    assert "refresh-secret" not in envelope.ciphertext_b64url
    assert "ingress-secret" not in envelope.ciphertext_b64url
    assert cipher.decrypt(envelope, aad=aad) == (
        "refresh-secret",
        "ingress-secret",
    )


def test_provider_secret_envelope_is_bound_to_authority_context():
    cipher = ProviderSecretCipher(_key())
    envelope = cipher.encrypt(
        refresh_token="refresh-secret",
        integration_bearer="ingress-secret",
        aad=b"owner-a",
    )

    with pytest.raises(Exception):
        cipher.decrypt(envelope, aad=b"owner-b")


@pytest.mark.parametrize("value", ["", "abc", "!" * 43])
def test_provider_secret_cipher_rejects_invalid_keys(value):
    with pytest.raises(ValueError, match="PROVIDER_AUTHORIZATION_KEY_INVALID"):
        ProviderSecretCipher(value)
