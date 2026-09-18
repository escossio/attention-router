import hashlib

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from attention_router.security.device_keys import (
    DeviceKeyInvalid,
    DeviceSignatureInvalid,
    encode_unpadded_base64url,
    parse_p256_spki_b64url,
    verify_p256_sha256_digest,
)


def _spki(public_key) -> bytes:
    return public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def test_parse_accepts_canonical_p256_spki_and_derives_fingerprint():
    private = ec.generate_private_key(ec.SECP256R1())
    raw = _spki(private.public_key())

    parsed, fingerprint = parse_p256_spki_b64url(
        encode_unpadded_base64url(raw)
    )

    assert parsed == raw
    assert fingerprint == "sha256:" + hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(
    "public_key",
    [
        ec.generate_private_key(ec.SECP384R1()).public_key(),
        rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key(),
    ],
)
def test_parse_rejects_non_p256_keys(public_key):
    with pytest.raises(DeviceKeyInvalid):
        parse_p256_spki_b64url(encode_unpadded_base64url(_spki(public_key)))


@pytest.mark.parametrize("value", ["", "!", "A" * 79, "A" * 2049])
def test_parse_rejects_malformed_or_out_of_bounds_spki(value):
    with pytest.raises(DeviceKeyInvalid):
        parse_p256_spki_b64url(value)


def test_verify_accepts_android_sha256with_ecdsa_shape():
    private = ec.generate_private_key(ec.SECP256R1())
    spki = _spki(private.public_key())
    challenge = b"synthetic-device-bootstrap-challenge"
    digest = hashlib.sha256(challenge).hexdigest()
    signature = private.sign(challenge, ec.ECDSA(hashes.SHA256()))

    verify_p256_sha256_digest(
        public_key_spki=spki,
        challenge_digest_hex=digest,
        signature=signature,
    )


def test_verify_rejects_wrong_key_or_digest():
    private = ec.generate_private_key(ec.SECP256R1())
    other = ec.generate_private_key(ec.SECP256R1())
    challenge = b"synthetic-device-bootstrap-challenge"
    signature = private.sign(challenge, ec.ECDSA(hashes.SHA256()))

    with pytest.raises(DeviceSignatureInvalid):
        verify_p256_sha256_digest(
            public_key_spki=_spki(other.public_key()),
            challenge_digest_hex=hashlib.sha256(challenge).hexdigest(),
            signature=signature,
        )

    with pytest.raises(DeviceSignatureInvalid):
        verify_p256_sha256_digest(
            public_key_spki=_spki(private.public_key()),
            challenge_digest_hex=hashlib.sha256(b"other").hexdigest(),
            signature=signature,
        )
