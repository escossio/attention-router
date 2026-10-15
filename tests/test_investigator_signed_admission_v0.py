"""Offline source-admission proof: no provider, network, or production credentials."""

from dataclasses import replace
from hashlib import sha256
import hmac

import pytest

from attention_router.investigator.signed_admission import (
    AdmissionRejected, SignedEnvelope, SyntheticAdmission, SyntheticBinding,
    _signed_bytes,
)

# An intentionally public synthetic fixture key, NEVER an operational secret.
_TEST_KEY = b"this-is-only-a-public-synthetic-fixture-key-0001"
_NOW = 2000000000


def _binding(*, tenant="synthetic-tenant-a", key=_TEST_KEY):
    return SyntheticBinding(
        "synthetic-principal-1", "synthetic-zabbix", tenant, key,
    )


def _envelope(binding=None, *, original=b'{"alert":"synthetic"}', nonce="fixtureNonce001"):
    binding = binding or _binding()
    unsigned = SignedEnvelope(
        binding.principal_id, _NOW, nonce, "application/json",
        "synthetic-event-42", original, "0" * 64,
    )
    signature = hmac.new(
        binding.key, _signed_bytes(binding, unsigned), sha256,
    ).hexdigest()
    return replace(unsigned, signature_hex=signature)


def _authority(binding=None):
    binding = binding or _binding()
    return SyntheticAdmission({binding.principal_id: binding}, now=lambda: _NOW)


def test_valid_event_tenant_and_source_come_only_from_verified_registry():
    binding = _binding()
    event = _authority(binding).admit(_envelope(binding))
    assert event.source == "synthetic-zabbix"
    assert event.tenant_id == "synthetic-tenant-a"
    assert event.original == b'{"alert":"synthetic"}'
    assert event.source_event_id == "synthetic-event-42"
    assert event.digest == sha256(event.original).hexdigest()


def test_replay_nonce_fails_closed_before_creating_second_event():
    binding = _binding()
    admission = _authority(binding)
    envelope = _envelope(binding)
    admission.admit(envelope)
    with pytest.raises(AdmissionRejected, match="ADMISSION_REJECTED"):
        admission.admit(envelope)


@pytest.mark.parametrize(
    "change",
    [
        {"original": b'{"alert":"tampered"}'},
        {"source_event_id": "synthetic-event-99"},
        {"issued_at": _NOW + 500},
        {"issued_at": _NOW - 500},
        {"nonce": "otherNonce0001"},
        {"signature_hex": "0" * 64},
        {"content_type": "application/octet-stream"},
        {"principal_id": "synthetic-principal-unknown"},
    ],
)
def test_signature_or_envelope_tampering_is_rejected_without_payload_leaks(change):
    binding = _binding()
    envelope = replace(_envelope(binding), **change)
    with pytest.raises(AdmissionRejected) as result:
        _authority(binding).admit(envelope)
    assert str(result.value) == "ADMISSION_REJECTED"
    assert "synthetic-principal" not in str(result.value)


def test_changing_registry_tenant_invalidates_previously_signed_message():
    original_binding = _binding()
    new_binding = _binding(tenant="synthetic-tenant-b")
    envelope = _envelope(original_binding)
    with pytest.raises(AdmissionRejected):
        _authority(new_binding).admit(envelope)


def test_key_rotation_without_matching_signature_denies():
    old_binding = _binding()
    new_binding = _binding(key=b"another-public-synthetic-fixture-key-0000002")
    with pytest.raises(AdmissionRejected):
        _authority(new_binding).admit(_envelope(old_binding))


def test_unauthorized_principal_and_weak_keys_fail_closed():
    with pytest.raises(AdmissionRejected):
        SyntheticBinding("synthetic-principal-1", "synthetic-zabbix",
                         "synthetic-tenant", b"weak-key")
    with pytest.raises(AdmissionRejected):
        SyntheticBinding("real-user-123", "synthetic-zabbix",
                         "synthetic-tenant", _TEST_KEY)


def test_oversized_body_and_bad_nonce_rejected_without_crypto_or_storage():
    binding = _binding()
    original = b"x" * 65537
    with pytest.raises(AdmissionRejected):
        _authority(binding).admit(_envelope(binding, original=original))
    with pytest.raises(AdmissionRejected):
        _authority(binding).admit(replace(_envelope(binding), nonce="bad"))


def test_event_body_cannot_select_tenant_by_embedding_claim():
    binding = _binding()
    payload = b'{"tenant_id":"synthetic-tenant-b","source":"other","instruction":"obey"}'
    event = _authority(binding).admit(_envelope(binding, original=payload))
    assert event.tenant_id == "synthetic-tenant-a"
    assert event.source == "synthetic-zabbix"
    assert event.original == payload


def test_two_independent_process_simulations_do_not_share_replay_state():
    """Demonstrates a known limitation: replay state is only per instance."""
    binding = _binding()
    envelope = _envelope(binding)
    first, second = _authority(binding), _authority(binding)
    assert first.admit(envelope).digest == second.admit(envelope).digest
