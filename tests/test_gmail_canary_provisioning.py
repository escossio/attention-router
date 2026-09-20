from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import stat

import pytest

from attention_router.integrations.gmail_canary_provisioning import (
    build_gmail_canary_installation,
    provision_gmail_canary,
    write_secret_file,
)
from attention_router.integrations.tenant_binding import (
    INBOUND_SCOPE,
    credential_digest,
)


def test_build_gmail_canary_installation_is_bounded_and_digest_only():
    stamp = datetime(2026, 9, 20, 23, 0, tzinfo=UTC)

    installation = build_gmail_canary_installation(
        tenant_id="tenant-1",
        audience="andy-integration-ingress",
        instance_id="gmail-primary",
        account_id="owner@example.invalid",
        lifetime_hours=24,
        now=stamp,
    )

    assert installation.binding.kind == "CHANNEL"
    assert installation.binding.name == "channel.email"
    assert installation.binding.active is True
    assert installation.binding.scopes == frozenset({INBOUND_SCOPE})
    assert installation.credential.binding_id == installation.binding.binding_id
    assert installation.credential.scopes == installation.binding.scopes
    assert installation.credential.revoked is False
    assert installation.credential.digest == credential_digest(
        installation.raw_secret
    )
    assert installation.raw_secret not in repr(installation.credential)
    assert installation.raw_secret not in repr(installation)
    assert installation.credential.expires_at > stamp
    assert len(installation.raw_secret) >= 43


@pytest.mark.parametrize("hours", [0, 169])
def test_build_gmail_canary_rejects_out_of_range_lifetime(hours):
    with pytest.raises(
        ValueError,
        match="GMAIL_CANARY_CREDENTIAL_LIFETIME_OUT_OF_RANGE",
    ):
        build_gmail_canary_installation(
            tenant_id="tenant-1",
            audience="andy-integration-ingress",
            instance_id="gmail-primary",
            account_id=None,
            lifetime_hours=hours,
        )


def test_write_secret_file_is_create_once_and_mode_0600(tmp_path):
    target = tmp_path / "gmail-canary.env"

    write_secret_file(target.resolve(), "secret-value")

    assert target.read_text() == (
        "ATTENTION_ROUTER_INTEGRATION_BEARER=secret-value\n"
    )
    assert stat.S_IMODE(target.stat().st_mode) == 0o600

    with pytest.raises(FileExistsError):
        write_secret_file(target.resolve(), "different-secret")

    assert target.read_text() == (
        "ATTENTION_ROUTER_INTEGRATION_BEARER=secret-value\n"
    )


def test_write_secret_file_requires_absolute_existing_parent(tmp_path):
    with pytest.raises(
        ValueError,
        match="GMAIL_CANARY_SECRET_PATH_MUST_BE_ABSOLUTE",
    ):
        write_secret_file(Path("relative.env"), "secret")

    with pytest.raises(
        ValueError,
        match="GMAIL_CANARY_SECRET_PARENT_NOT_FOUND",
    ):
        write_secret_file(
            (tmp_path / "missing" / "secret.env").resolve(),
            "secret",
        )


def test_provisioning_removes_secret_file_if_database_operation_fails(
    tmp_path,
    monkeypatch,
):
    target = (tmp_path / "gmail-canary.env").resolve()

    def fail_provision(*_args, **_kwargs):
        raise RuntimeError("synthetic database failure")

    monkeypatch.setattr(
        "attention_router.integrations.gmail_canary_provisioning.provision_installation",
        fail_provision,
    )

    with pytest.raises(RuntimeError, match="synthetic database failure"):
        provision_gmail_canary(
            object(),
            tenant_id="tenant-1",
            audience="andy-integration-ingress",
            instance_id="gmail-primary",
            account_id=None,
            secret_path=target,
        )

    assert not target.exists()
