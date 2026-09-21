from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True, slots=True)
class ProviderSecretEnvelope:
    nonce_b64url: str
    ciphertext_b64url: str
    key_version: str = "v1"


class ProviderSecretCipher:
    def __init__(self, key_b64url: str):
        try:
            padded = key_b64url + "=" * (-len(key_b64url) % 4)
            key = base64.urlsafe_b64decode(padded.encode("ascii"))
        except Exception as exc:
            raise ValueError("PROVIDER_AUTHORIZATION_KEY_INVALID") from exc
        if len(key) != 32:
            raise ValueError("PROVIDER_AUTHORIZATION_KEY_INVALID")
        self._aead = AESGCM(key)

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    @staticmethod
    def _decode(value: str) -> bytes:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii"))

    def encrypt(
        self,
        *,
        refresh_token: str,
        integration_bearer: str,
        aad: bytes,
    ) -> ProviderSecretEnvelope:
        if not refresh_token or not integration_bearer:
            raise ValueError("PROVIDER_SECRET_EMPTY")
        payload = json.dumps(
            {
                "refresh_token": refresh_token,
                "integration_bearer": integration_bearer,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        nonce = os.urandom(12)
        ciphertext = self._aead.encrypt(nonce, payload, aad)
        return ProviderSecretEnvelope(
            nonce_b64url=self._encode(nonce),
            ciphertext_b64url=self._encode(ciphertext),
        )

    def decrypt(
        self,
        envelope: ProviderSecretEnvelope,
        *,
        aad: bytes,
    ) -> tuple[str, str]:
        if envelope.key_version != "v1":
            raise ValueError("PROVIDER_SECRET_KEY_VERSION_UNSUPPORTED")
        plaintext = self._aead.decrypt(
            self._decode(envelope.nonce_b64url),
            self._decode(envelope.ciphertext_b64url),
            aad,
        )
        decoded = json.loads(plaintext.decode("utf-8"))
        refresh_token = decoded.get("refresh_token")
        integration_bearer = decoded.get("integration_bearer")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise ValueError("PROVIDER_SECRET_INVALID")
        if not isinstance(integration_bearer, str) or not integration_bearer:
            raise ValueError("PROVIDER_SECRET_INVALID")
        return refresh_token, integration_bearer
