"""Offline, provider-free proof of the host bootstrap auth patch contract."""

from hashlib import sha256

import pytest

from scripts.operations import whatsapp_bootstrap_auth_patch as patch


@pytest.mark.parametrize(("native", "app", "eligible"), [
    ("CONNECTED", None, True),
    ("CONNECTED", "CONNECTED", True),
    ("CONNECTED", "OPENING", False),
    ("CONNECTED", "UNPAIRED", False),
    ("CONNECTED", "CONNECTING", False),
    ("CONNECTED", "", False),
    ("CONNECTING", None, False),
    (None, "CONNECTED", False),
    ("CONNECTED", False, False),
])
def test_browser_auth_state_gate(native, app, eligible):
    assert patch.browser_auth_state_eligible({"nativeState": native, "appState": app}) is eligible


@pytest.mark.parametrize("value", [[], None, 1, "CONNECTED", True, {}, {"nativeState": "CONNECTED"}])
def test_browser_auth_rejects_malformed_or_missing_fields(value):
    assert patch.browser_auth_state_eligible(value) is (value == {"nativeState": "CONNECTED"})


def test_pinned_replacement_reproduces_exact_bytes():
    original = ("def fake():\n" + patch.SOURCE_ANCHOR + "\n").encode()
    expected = ("def fake():\n" + patch.REPAIRED_ANCHOR + "\n").encode()
    actual = patch.patch_bytes(
        original,
        source_sha256=sha256(original).hexdigest(),
        result_sha256=sha256(expected).hexdigest(),
    )
    assert actual == expected


def test_mutated_source_sha_fails_closed():
    with pytest.raises(patch.BootstrapPatchRejected, match="SOURCE_SHA256_MISMATCH"):
        patch.patch_bytes(b"unexpected")


def test_duplicate_source_anchor_fails_closed():
    original = (patch.SOURCE_ANCHOR + "\n" + patch.SOURCE_ANCHOR).encode()
    with pytest.raises(patch.BootstrapPatchRejected, match="SOURCE_ANCHOR_MISMATCH"):
        patch.patch_bytes(original, source_sha256=sha256(original).hexdigest(), result_sha256="0" * 64)


def test_source_with_unexpected_patch_is_rejected():
    original = patch.REPAIRED_ANCHOR.encode()
    with pytest.raises(patch.BootstrapPatchRejected, match="SOURCE_ANCHOR_MISMATCH"):
        patch.patch_bytes(original, source_sha256=sha256(original).hexdigest(), result_sha256="0" * 64)


def test_expected_result_sha_is_enforced():
    original = patch.SOURCE_ANCHOR.encode()
    with pytest.raises(patch.BootstrapPatchRejected, match="RESULT_SHA256_MISMATCH"):
        patch.patch_bytes(original, source_sha256=sha256(original).hexdigest(), result_sha256="0" * 64)
