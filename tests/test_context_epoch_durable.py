"""Explicitly provisioned, backup-independent epoch CAS canary checks."""

from dataclasses import replace
from hmac import compare_digest

import pytest

from attention_router.application.context_epoch import ContextEpoch, sign_state
from attention_router.config import settings
from attention_router.infrastructure.context_epoch_durable import DurableEpochAuthority


KEY = b"synthetic-epoch-key-never-production-00000000"


def test_durable_authority_read_cas_and_reopen(tmp_path):
    path = tmp_path / "epoch.sqlite"
    assert not path.exists()
    with pytest.raises(ValueError, match="MISSING"):
        DurableEpochAuthority(path, authority_id="test-anchor", key=KEY)
    anchor = DurableEpochAuthority.provision(
        path, authority_id="test-anchor", key=KEY,
        domain="synthetic-domain", checkpoint="checkpoint-zero",
    )
    current, signature = anchor.read("synthetic-domain", "challenge-one")
    assert compare_digest(signature, sign_state(KEY, current, "challenge-one"))
    assert path.stat().st_mode & 0o077 == 0

    reopen = DurableEpochAuthority(path, authority_id="test-anchor", key=KEY)
    proposed = replace(current, revision=1, checkpoint="checkpoint-one")
    anchor.advance(current, proposed)
    with pytest.raises(ValueError, match="CAS_CONFLICT"):
        reopen.advance(current, proposed)
    saved, signature = reopen.read("synthetic-domain", "challenge-two")
    assert saved == proposed
    assert compare_digest(signature, sign_state(KEY, saved, "challenge-two"))
    with pytest.raises(OSError, match="DOMAIN_UNKNOWN"):
        anchor.read("missing-domain", "challenge")


def test_refuse_duplicate_and_unsafe_permissions(tmp_path):
    path = tmp_path / "epoch.sqlite"
    anchor = DurableEpochAuthority.provision(
        path, authority_id="test-anchor", key=KEY, domain="one", checkpoint="v1",
    )
    with pytest.raises(ValueError, match="ALREADY_PROVISIONED"):
        DurableEpochAuthority.provision(
            path, authority_id="test-anchor", key=KEY, domain="one", checkpoint="v1",
        )
    path.chmod(0o644)
    with pytest.raises(OSError, match="READ_UNAVAILABLE"):
        anchor.read("one", "challenge")


def test_runtime_epoch_canary_requires_explicit_flag_and_durable_authority(tmp_path, monkeypatch):
    path = tmp_path / "epoch.sqlite"
    anchor = DurableEpochAuthority.provision(
        path, authority_id="test-anchor", key=KEY, domain="one", checkpoint="v1",
    )
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "context_epoch_authority_canary_enabled", False)
    with pytest.raises(ValueError, match="CONFIGURATION_DENIED"):
        ContextEpoch(anchor, authority_id="test-anchor", environment="test", key=KEY)

    monkeypatch.setattr(settings, "context_epoch_authority_canary_enabled", True)
    # The operational canary now requires every owner/tenant/account bound gate.
    monkeypatch.setattr(settings, "staged_context_private_canary_enabled", True)
    monkeypatch.setattr(settings, "client_session_enabled", True)
    monkeypatch.setattr(settings, "cognitive_runtime_canary_tenant_id", "synthetic-tenant")
    monkeypatch.setattr(settings, "persistent_memory_canary_binding_id", "synthetic-binding")
    monkeypatch.setattr(settings, "staged_context_canary_owner_id", "synthetic-owner")
    monkeypatch.setattr(settings, "staged_context_canary_source_account", "synthetic-account")
    assert ContextEpoch(anchor, authority_id="test-anchor", environment="test", key=KEY)
    with pytest.raises(ValueError, match="CONFIGURATION_DENIED"):
        ContextEpoch(object(), authority_id="test-anchor", environment="test", key=KEY)
