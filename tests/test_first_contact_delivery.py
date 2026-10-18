"""Synthetic delivery receipts: no browser, provider, database or network."""

from dataclasses import FrozenInstanceError, replace
import hashlib
import hmac
import json
from urllib import error

import pytest

from attention_router.adapters.first_contact_delivery import (
    FirstContactDeliveryAdapter,
    FirstContactDeliveryError,
    VerifiedDeliveryProof,
    verify_delivery_response,
)


SECRET = "synthetic-first-contact-proof-secret"
NOW = 1_790_000_000
NONCE = "ab" * 32
SCOPE = {
    "schema_version": "1", "contract_version": "first-contact-v1",
    "attempt_id": "attempt-test", "presentation_version": "first-contact-v1",
    "tenant_id": "tenant-test", "source": "wwebjs", "channel": "whatsapp",
    "source_account": "account-test", "canonical_peer_hash": "cd" * 32,
    "execution_intent_id": "intent-test", "outbox_id": "outbox-test",
    "idempotency_key": "execution:intent-test", "text_hash": "ef" * 32,
}


def signed(body=None, *, nonce=NONCE, timestamp=NOW, secret=SECRET, raw=None):
    if body is None:
        body = {**SCOPE, "nonce": nonce, "issued_at": timestamp, "status": "SENT",
                "message_reference": "synthetic-reference", "ack": 2,
                "ack_at": "2026-09-01T00:00:00Z"}
    raw = raw if raw is not None else json.dumps(body, separators=(",", ":")).encode()
    prefix = f"first-contact-delivery:v1\nresponse\n{timestamp}\n{nonce}\n".encode()
    headers = {"X-Attention-Proof-Timestamp": str(timestamp),
               "X-Attention-Proof-Signature": "sha256=" + hmac.new(
                   secret.encode(), prefix + raw, hashlib.sha256).hexdigest()}
    return raw, headers


def verify(raw, headers, **kwargs):
    return verify_delivery_response(raw, headers, secret=SECRET, expected_nonce=NONCE,
                                    expected_scope=SCOPE, now=NOW, **kwargs)


def test_verified_receipt_is_closed_frozen_and_binds_raw_digest():
    raw, headers = signed()
    proof = verify(raw, headers)
    assert isinstance(proof, VerifiedDeliveryProof)
    assert proof.ack == 2 and proof.outbox_id == SCOPE["outbox_id"]
    assert proof.body_sha256 == hashlib.sha256(raw).hexdigest()
    with pytest.raises(FrozenInstanceError):
        proof.ack = 4
    with pytest.raises(FirstContactDeliveryError, match="VERIFIER_REQUIRED"):
        replace(proof, ack=4)
    with pytest.raises(FirstContactDeliveryError, match="VERIFIER_REQUIRED"):
        VerifiedDeliveryProof(**json.loads(raw), body_sha256="00" * 32)


@pytest.mark.parametrize("field", sorted(SCOPE))
def test_every_effect_and_namespace_field_is_correlated(field):
    body = json.loads(signed()[0])
    body[field] += "-different"
    with pytest.raises(FirstContactDeliveryError, match="SCOPE_MISMATCH"):
        verify(*signed(body))


@pytest.mark.parametrize("status", ["UNKNOWN", "SENDING", "SENT", "FAILED"])
def test_unknown_or_unacknowledged_is_typed_evidence_never_invented_ack(status):
    body = json.loads(signed()[0])
    body.update(status=status, ack=None, ack_at=None,
                message_reference="synthetic-reference" if status == "SENT" else None)
    proof = verify(*signed(body))
    assert proof.status == status and proof.ack is None and proof.ack_at is None


@pytest.mark.parametrize("offset", [-61, 6])
def test_replayed_or_future_proof_is_denied(offset):
    with pytest.raises(FirstContactDeliveryError, match="NOT_FRESH"):
        verify(*signed(timestamp=NOW + offset))


def test_tampered_raw_body_wrong_domain_nonce_and_secret_are_denied():
    raw, headers = signed()
    with pytest.raises(FirstContactDeliveryError, match="AUTH_INVALID"):
        verify(raw + b" ", headers)
    with pytest.raises(FirstContactDeliveryError, match="AUTH_INVALID"):
        verify(*signed(secret="different-secret"))
    with pytest.raises(FirstContactDeliveryError, match="AUTH_INVALID"):
        verify(*signed(nonce="ff" * 32))
    prefix = f"first-contact-delivery:v1\nrequest\n{NOW}\n{NONCE}\n".encode()
    headers["X-Attention-Proof-Signature"] = "sha256=" + hmac.new(
        SECRET.encode(), prefix + raw, hashlib.sha256).hexdigest()
    with pytest.raises(FirstContactDeliveryError, match="AUTH_INVALID"):
        verify(raw, headers)


@pytest.mark.parametrize("change,reason", [
    ({"ack": True}, "ACK_INVALID"), ({"ack": 5}, "ACK_INVALID"),
    ({"ack": -1}, "ACK_INVALID"), ({"ack": "2"}, "ACK_INVALID"),
    ({"ack_at": None}, "ACK_INVALID"), ({"ack": None}, "ACK_INVALID"),
    ({"ack_at": "2026-09-01T00:00:00"}, "ACK_INVALID"),
    ({"ack_at": "2099-01-01T00:00:00Z"}, "ACK_INVALID"),
    ({"message_reference": None}, "REFERENCE_INVALID"),
    ({"message_reference": "a\nb"}, "REFERENCE_INVALID"),
    ({"message_reference": "a" * 513}, "REFERENCE_INVALID"),
    ({"issued_at": True}, "CHALLENGE_MISMATCH"),
    ({"nonce": "ff" * 32}, "CHALLENGE_MISMATCH"),
    ({"status": "CONFIRMED"}, "STATUS_INVALID"),
    ({"status": []}, "STATUS_INVALID"),
    ({"unexpected": "value"}, "SHAPE_INVALID"),
])
def test_signed_malformed_evidence_is_still_denied(change, reason):
    body = json.loads(signed()[0])
    body.update(change)
    with pytest.raises(FirstContactDeliveryError, match=reason):
        verify(*signed(body))


def test_duplicate_keys_headers_and_oversized_bodies_are_denied():
    raw, headers = signed(raw=b'{"nonce":"a","nonce":"b"}')
    with pytest.raises(FirstContactDeliveryError, match="JSON_INVALID"):
        verify(raw, headers)
    raw, headers = signed()
    headers["x-attention-proof-timestamp"] = str(NOW)
    with pytest.raises(FirstContactDeliveryError, match="HEADERS_INVALID"):
        verify(raw, headers)
    with pytest.raises(FirstContactDeliveryError, match="INPUT_INVALID"):
        verify(*signed(raw=b" " * 16385))


class FakeResponse:
    status = 200

    def __init__(self, raw, headers):
        self.raw, self.headers = raw, headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.raw[:limit]


def test_probe_uses_fixed_trusted_origin_separate_request_hmac_and_nonce():
    calls = []

    def opener(req, timeout):
        calls.append(req)
        assert req.full_url == "http://transport.invalid:3000/internal/first-contact/delivery"
        assert req.method == "POST" and timeout == 3
        assert json.loads(req.data) == SCOPE
        prefix = f"first-contact-delivery:v1\nrequest\n{NOW}\n{NONCE}\n".encode()
        expected = "sha256=" + hmac.new(SECRET.encode(), prefix + req.data,
                                        hashlib.sha256).hexdigest()
        assert req.get_header("X-attention-signature") == expected
        return FakeResponse(*signed())

    adapter = FirstContactDeliveryAdapter("http://transport.invalid:3000/internal/send",
                                         SECRET, 3, opener=opener, clock=lambda: NOW,
                                         nonce_factory=lambda: NONCE)
    assert adapter.probe(SCOPE).ack == 2
    assert len(calls) == 1


def test_probe_denies_http_failure_and_never_follows_redirects():
    def opener(req, **kwargs):
        raise error.HTTPError(req.full_url, 302, "redirect", {}, None)

    adapter = FirstContactDeliveryAdapter("http://transport.invalid/internal/send", SECRET,
                                         opener=opener, clock=lambda: NOW,
                                         nonce_factory=lambda: NONCE)
    with pytest.raises(FirstContactDeliveryError, match="UNAVAILABLE"):
        adapter.probe(SCOPE)


@pytest.mark.parametrize("url", ["file:///tmp/proof", "http://user:pass@transport.invalid",
                                     "http://transport.invalid/?secret=1", "http:///#fragment"])
def test_untrusted_origin_configuration_is_denied(url):
    with pytest.raises(FirstContactDeliveryError, match="CONFIGURATION_INVALID"):
        FirstContactDeliveryAdapter(url, SECRET)


def test_invalid_scope_is_denied_before_opener_runs():
    adapter = FirstContactDeliveryAdapter("http://transport.invalid", SECRET,
                                         opener=lambda *args, **kwargs: pytest.fail("network"))
    with pytest.raises(FirstContactDeliveryError, match="SCOPE_INVALID"):
        adapter.probe({**SCOPE, "idempotency_key": "arbitrary"})
