"""Provider-neutral P-256 device-key parsing and possession verification."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils


_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")


class DeviceKeyInvalid(ValueError):
    pass


class DeviceSignatureInvalid(ValueError):
    pass


def decode_unpadded_base64url(value: str, *, minimum: int, maximum: int) -> bytes:
    if (
        not isinstance(value, str)
        or not minimum <= len(value) <= maximum
        or not _B64URL.fullmatch(value)
    ):
        raise DeviceKeyInvalid()
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, binascii.Error) as error:
        raise DeviceKeyInvalid() from error


def encode_unpadded_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def parse_p256_spki_b64url(value: str) -> tuple[bytes, str]:
    spki = decode_unpadded_base64url(value, minimum=80, maximum=2048)
    try:
        public_key = serialization.load_der_public_key(spki)
    except (ValueError, TypeError) as error:
        raise DeviceKeyInvalid() from error
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise DeviceKeyInvalid()
    if not isinstance(public_key.curve, ec.SECP256R1):
        raise DeviceKeyInvalid()
    canonical_spki = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if canonical_spki != spki:
        raise DeviceKeyInvalid()
    fingerprint = "sha256:" + hashlib.sha256(canonical_spki).hexdigest()
    return canonical_spki, fingerprint


def decode_signature_b64url(value: str) -> bytes:
    try:
        return decode_unpadded_base64url(value, minimum=64, maximum=512)
    except DeviceKeyInvalid as error:
        raise DeviceSignatureInvalid() from error


def verify_p256_sha256_digest(
    *,
    public_key_spki: bytes,
    challenge_digest_hex: str,
    signature: bytes,
) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", challenge_digest_hex):
        raise DeviceSignatureInvalid()
    try:
        public_key = serialization.load_der_public_key(public_key_spki)
    except (ValueError, TypeError) as error:
        raise DeviceSignatureInvalid() from error
    if (
        not isinstance(public_key, ec.EllipticCurvePublicKey)
        or not isinstance(public_key.curve, ec.SECP256R1)
    ):
        raise DeviceSignatureInvalid()
    digest = bytes.fromhex(challenge_digest_hex)
    try:
        public_key.verify(
            signature,
            digest,
            ec.ECDSA(utils.Prehashed(hashes.SHA256())),
        )
    except (InvalidSignature, ValueError) as error:
        raise DeviceSignatureInvalid() from error
