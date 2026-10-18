"""Read authenticated First Contact delivery evidence, without sending messages."""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import datetime
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Mapping
from urllib import error, parse, request


PROOF_PATH = "/internal/first-contact/delivery"
_DOMAIN = b"first-contact-delivery:v1"
_TOKEN = object()
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_SCOPE_FIELDS = frozenset({
    "schema_version", "contract_version", "attempt_id", "presentation_version",
    "tenant_id", "source", "channel", "source_account", "canonical_peer_hash",
    "execution_intent_id", "outbox_id", "idempotency_key", "text_hash",
})
_PROOF_FIELDS = _SCOPE_FIELDS | {"nonce", "issued_at", "status", "message_reference", "ack", "ack_at"}


class FirstContactDeliveryError(ValueError):
    """Bounded reason codes only: no secrets, body, recipient or reference."""


def _bounded(value: object, limit: int = 180) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= limit and value == value.strip()
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _scope(value: Mapping) -> dict:
    if not isinstance(value, Mapping):
        raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_SCOPE_INVALID")
    result = dict(value)
    if (set(result) != _SCOPE_FIELDS or not all(_bounded(item) for item in result.values())
            or result["schema_version"] != "1" or result["contract_version"] != "first-contact-v1"
            or result["source"] != "wwebjs" or result["channel"] != "whatsapp"
            or result["idempotency_key"] != f"execution:{result['execution_intent_id']}"
            or not _HEX.fullmatch(result["canonical_peer_hash"])
            or not _HEX.fullmatch(result["text_hash"])):
        raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_SCOPE_INVALID")
    return result


def _signature(secret: str, direction: str, timestamp: str, nonce: str, body: bytes) -> str:
    prefix = b"\n".join((_DOMAIN, direction.encode(), timestamp.encode(), nonce.encode())) + b"\n"
    return "sha256=" + hmac.new(secret.encode(), prefix + body, hashlib.sha256).hexdigest()


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_JSON_INVALID")
        result[key] = value
    return result


@dataclass(frozen=True)
class VerifiedDeliveryProof:
    """An authenticated snapshot; confirmation still needs exact DB scope/ACK."""

    schema_version: str
    contract_version: str
    attempt_id: str
    presentation_version: str
    tenant_id: str
    source: str
    channel: str
    source_account: str
    canonical_peer_hash: str
    execution_intent_id: str
    outbox_id: str
    idempotency_key: str
    text_hash: str
    nonce: str
    issued_at: int
    status: str
    message_reference: str | None
    ack: int | None
    ack_at: str | None
    body_sha256: str
    _validation_token: InitVar[object] = None

    def __post_init__(self, _validation_token):
        if _validation_token is not _TOKEN:
            raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_VERIFIER_REQUIRED")


def verify_delivery_response(
    raw_body: bytes,
    headers: Mapping,
    *,
    secret: str,
    expected_nonce: str,
    expected_scope: Mapping,
    now: float | None = None,
) -> VerifiedDeliveryProof:
    """Authenticate raw bytes, then validate the caller's exact pending effect."""
    scope = _scope(expected_scope)
    if (not isinstance(raw_body, bytes) or len(raw_body) > 16384 or not secret
            or not isinstance(expected_nonce, str) or not _HEX.fullmatch(expected_nonce)):
        raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_INPUT_INVALID")
    lowered = {}
    for key, value in headers.items():
        name = str(key).lower()
        if name in lowered:
            raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_HEADERS_INVALID")
        lowered[name] = value
    timestamp = lowered.get("x-attention-proof-timestamp")
    supplied = lowered.get("x-attention-proof-signature")
    if (not isinstance(timestamp, str) or not re.fullmatch(r"[0-9]{1,12}", timestamp)
            or not isinstance(supplied, str) or not re.fullmatch(r"sha256=[0-9a-f]{64}", supplied)):
        raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_AUTH_INVALID")
    age = (time.time() if now is None else now) - int(timestamp)
    if age < -5 or age > 60:
        raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_NOT_FRESH")
    expected_signature = _signature(secret, "response", timestamp, expected_nonce, raw_body)
    if not hmac.compare_digest(supplied, expected_signature):
        raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_AUTH_INVALID")
    try:
        body = json.loads(raw_body.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError) as exc:
        raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_JSON_INVALID") from exc
    if not isinstance(body, dict) or set(body) != _PROOF_FIELDS:
        raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_SHAPE_INVALID")
    if any(body[key] != value for key, value in scope.items()):
        raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_SCOPE_MISMATCH")
    if (body["nonce"] != expected_nonce or type(body["issued_at"]) is not int
            or body["issued_at"] != int(timestamp)):
        raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_CHALLENGE_MISMATCH")
    if (not isinstance(body["status"], str)
            or body["status"] not in {"UNKNOWN", "SENDING", "SENT", "FAILED"}):
        raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_STATUS_INVALID")
    ack, ack_at, reference = body["ack"], body["ack_at"], body["message_reference"]
    if (ack is not None and (type(ack) is not int or not 0 <= ack <= 4)):
        raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_ACK_INVALID")
    if reference is not None and not _bounded(reference, 512):
        raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_REFERENCE_INVALID")
    if (ack is None) != (ack_at is None):
        raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_ACK_INVALID")
    if ack_at is not None:
        try:
            parsed = datetime.fromisoformat(ack_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.timestamp() > int(timestamp) + 5:
                raise ValueError()
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_ACK_INVALID") from exc
    if body["status"] == "SENT" and ack is not None and ack >= 2 and not reference:
        raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_REFERENCE_INVALID")
    return VerifiedDeliveryProof(**body, body_sha256=hashlib.sha256(raw_body).hexdigest(),
                                 _validation_token=_TOKEN)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class FirstContactDeliveryAdapter:
    """Uses the existing trusted Transport origin, with a separate HMAC domain."""

    def __init__(self, url: str | None = None, secret: str | None = None,
                 timeout: float = 5.0, *, opener=None, clock=time.time,
                 nonce_factory=None):
        if url is None or secret is None:
            from attention_router.config import settings

            url = url if url is not None else settings.local_transport_outbound_url
            secret = secret if secret is not None else settings.internal_ingress_hmac_secret
        parsed = parse.urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or not secret or not 0 < timeout <= 30):
            raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_CONFIGURATION_INVALID")
        self.url = parse.urlunsplit((parsed.scheme, parsed.netloc, PROOF_PATH, "", ""))
        self._secret = secret
        self.timeout = timeout
        self._open = opener or request.build_opener(_NoRedirect()).open
        self._clock = clock
        self._nonce = nonce_factory or (lambda: secrets.token_hex(32))

    def probe(self, expected_scope: Mapping) -> VerifiedDeliveryProof:
        scope = _scope(expected_scope)
        nonce = self._nonce()
        if not isinstance(nonce, str) or not _HEX.fullmatch(nonce):
            raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_NONCE_INVALID")
        timestamp = str(int(self._clock()))
        raw = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "X-Attention-Timestamp": timestamp,
                   "X-Attention-Nonce": nonce,
                   "X-Attention-Signature": _signature(self._secret, "request", timestamp, nonce, raw)}
        req = request.Request(self.url, data=raw, headers=headers, method="POST")
        try:
            with self._open(req, timeout=self.timeout) as response:
                if response.status != 200:
                    raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_HTTP_DENIED")
                body = response.read(16385)
                return verify_delivery_response(body, response.headers, secret=self._secret,
                                                expected_nonce=nonce, expected_scope=scope,
                                                now=self._clock())
        except (error.HTTPError, error.URLError, TimeoutError, OSError) as exc:
            raise FirstContactDeliveryError("FIRST_CONTACT_PROOF_UNAVAILABLE") from exc
