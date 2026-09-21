from __future__ import annotations

from datetime import datetime, timezone
import stat

import pytest
from pydantic import ValidationError

from attention_router.application.platform.artifact_storage import (
    ArtifactStageInput,
    ArtifactStorageBackendUnavailable,
    ArtifactUnavailable,
    read_artifact_bytes,
    stage_artifact_receipt,
)
from attention_router.config import Settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID, TenantScopeError
from attention_router.domain.models import now_utc
from attention_router.infrastructure.artifact_models import ArtifactReceiptRow
from attention_router.infrastructure.artifact_store import (
    ARTIFACT_DIRECTORY_MODE,
    ARTIFACT_FILE_MODE,
    ArtifactStoreError,
    LocalArtifactStore,
)
from attention_router.infrastructure.models import TenantRow


RECEIVED_AT = datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc)
def _store(tmp_path, *, max_bytes=1024):
    return LocalArtifactStore(
        tmp_path / "artifacts",
        max_bytes=max_bytes,
    )


def _stage(
    session,
    store,
    *,
    tenant_id=DEFAULT_TENANT_ID,
    data=b"hello artifact",
    source_channel="email",
    external_receipt_id="gmail-message:attachment-1",
    original_filename="report.pdf",
):
    return stage_artifact_receipt(
        session,
        store,
        ArtifactStageInput(
            tenant_id=tenant_id,
            data=data,
            artifact_kind="document",
            mime_type="application/pdf",
            source_channel=source_channel,
            external_receipt_id=external_receipt_id,
            received_at=RECEIVED_AT,
            source_account="mailbox-1",
            original_filename=original_filename,
        ),
    )


def _second_tenant(session):
    tenant_id = "00000000-0000-4000-8000-000000000099"
    stamp = now_utc()
    session.add(
        TenantRow(
            id=tenant_id,
            slug="artifact_store_second_tenant",
            name="Artifact Store Second Tenant",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()
    return tenant_id


def test_local_store_round_trip_is_tenant_scoped(tmp_path):
    store = _store(tmp_path)
    payload = b"same bytes in two tenants"

    first = store.put_bytes("tenant-a", payload)
    second = store.put_bytes("tenant-b", payload)

    assert first.storage_reference == second.storage_reference
    assert first.content_sha256 == second.content_sha256
    first_path = store.path_for_ref(
        "tenant-a", first.storage_reference
    )
    second_path = store.path_for_ref(
        "tenant-b", second.storage_reference
    )
    assert first_path != second_path
    assert "tenant-a" not in str(first_path)
    assert "tenant-b" not in str(second_path)
    assert store.read_bytes(
        "tenant-a",
        first.storage_reference,
        expected_sha256=first.content_sha256,
        expected_size=first.size_bytes,
    ) == payload
    assert store.read_bytes(
        "tenant-b",
        second.storage_reference,
        expected_sha256=second.content_sha256,
        expected_size=second.size_bytes,
    ) == payload
def test_local_store_permissions_are_private_and_readable(tmp_path):
    store = _store(tmp_path)
    stored = store.put_bytes("tenant-a", b"permissions")
    path = store.path_for_ref(
        "tenant-a", stored.storage_reference
    )

    assert stat.S_IMODE(path.stat().st_mode) == ARTIFACT_FILE_MODE
    assert stat.S_IMODE(path.parent.stat().st_mode) == ARTIFACT_DIRECTORY_MODE
    assert stat.S_IMODE(store.root.stat().st_mode) == ARTIFACT_DIRECTORY_MODE


def test_local_store_supports_empty_opaque_object(tmp_path):
    store = _store(tmp_path)
    stored = store.put_bytes("tenant-a", b"")

    assert stored.size_bytes == 0
    assert store.read_bytes(
        "tenant-a",
        stored.storage_reference,
        expected_sha256=stored.content_sha256,
        expected_size=0,
    ) == b""


def test_local_store_rejects_oversize_and_invalid_inputs(tmp_path):
    store = _store(tmp_path, max_bytes=4)

    with pytest.raises(
        ArtifactStoreError,
        match="ARTIFACT_STORE_TOO_LARGE",
    ):
        store.put_bytes("tenant-a", b"12345")
    with pytest.raises(
        ArtifactStoreError,
        match="ARTIFACT_STORE_TENANT_INVALID",
    ):
        store.put_bytes("", b"x")
    with pytest.raises(
        ArtifactStoreError,
        match="ARTIFACT_STORE_BYTES_REQUIRED",
    ):
        store.put_bytes("tenant-a", bytearray(b"x"))
def test_local_store_detects_tampering(tmp_path):
    store = _store(tmp_path)
    stored = store.put_bytes("tenant-a", b"hello")
    path = store.path_for_ref(
        "tenant-a", stored.storage_reference
    )
    path.write_bytes(b"jello")

    with pytest.raises(
        ArtifactStoreError,
        match="ARTIFACT_STORE_HASH_MISMATCH",
    ):
        store.read_bytes(
            "tenant-a",
            stored.storage_reference,
            expected_sha256=stored.content_sha256,
            expected_size=stored.size_bytes,
        )


def test_local_store_rejects_symlink_object(tmp_path):
    store = _store(tmp_path)
    stored = store.put_bytes("tenant-a", b"original")
    path = store.path_for_ref(
        "tenant-a", stored.storage_reference
    )
    other = tmp_path / "other"
    other.write_bytes(b"original")
    path.unlink()
    path.symlink_to(other)

    with pytest.raises(ArtifactStoreError):
        store.read_bytes(
            "tenant-a",
            stored.storage_reference,
            expected_sha256=stored.content_sha256,
            expected_size=stored.size_bytes,
        )
def test_stage_registers_bytes_and_round_trips_by_artifact_id(
    session,
    tmp_path,
):
    store = _store(tmp_path)
    staged = _stage(
        session,
        store,
        original_filename="../../untrusted-name.pdf",
    )

    artifact = staged.registration.artifact
    assert artifact.storage_provider == store.storage_provider
    assert artifact.storage_reference == staged.stored.storage_reference
    assert artifact.content_sha256 == staged.stored.content_sha256
    assert artifact.size_bytes == staged.stored.size_bytes
    assert artifact.original_filename == "../../untrusted-name.pdf"
    assert staged.registration.artifact_created is True
    assert staged.registration.receipt_created is True

    payload = read_artifact_bytes(
        session,
        {store.storage_provider: store},
        tenant_id=DEFAULT_TENANT_ID,
        artifact_id=artifact.id,
    )
    assert payload == b"hello artifact"
    path = store.path_for_ref(
        DEFAULT_TENANT_ID,
        artifact.storage_reference,
    )
    assert "untrusted-name.pdf" not in str(path)
def test_staging_same_content_reuses_artifact_and_adds_receipt(
    session,
    tmp_path,
):
    store = _store(tmp_path)
    first = _stage(session, store)
    second = _stage(
        session,
        store,
        source_channel="whatsapp",
        external_receipt_id="wa-message:media-1",
    )

    assert (
        first.registration.artifact.id
        == second.registration.artifact.id
    )
    assert second.registration.artifact_created is False
    assert second.registration.receipt_created is True
    assert (
        session.query(ArtifactReceiptRow).count()
        == 2
    )


def test_staging_same_source_replay_is_idempotent(
    session,
    tmp_path,
):
    store = _store(tmp_path)
    first = _stage(session, store)
    replay = _stage(session, store)

    assert (
        replay.registration.artifact.id
        == first.registration.artifact.id
    )
    assert replay.registration.artifact_created is False
    assert replay.registration.receipt_created is False
    assert session.query(ArtifactReceiptRow).count() == 1
def test_internal_read_denies_cross_tenant_lookup(
    session,
    tmp_path,
):
    store = _store(tmp_path)
    staged = _stage(session, store)
    second_tenant = _second_tenant(session)

    with pytest.raises(
        TenantScopeError,
        match="ARTIFACT_NOT_FOUND",
    ):
        read_artifact_bytes(
            session,
            {store.storage_provider: store},
            tenant_id=second_tenant,
            artifact_id=staged.registration.artifact.id,
        )


def test_internal_read_requires_available_artifact(
    session,
    tmp_path,
):
    store = _store(tmp_path)
    staged = _stage(session, store)
    staged.registration.artifact.status = "DELETED"
    session.flush()

    with pytest.raises(
        ArtifactUnavailable,
        match="ARTIFACT_NOT_AVAILABLE",
    ):
        read_artifact_bytes(
            session,
            {store.storage_provider: store},
            tenant_id=DEFAULT_TENANT_ID,
            artifact_id=staged.registration.artifact.id,
        )
def test_internal_read_fails_closed_for_missing_backend(
    session,
    tmp_path,
):
    store = _store(tmp_path)
    staged = _stage(session, store)

    with pytest.raises(
        ArtifactStorageBackendUnavailable,
        match="ARTIFACT_STORAGE_BACKEND_UNAVAILABLE",
    ):
        read_artifact_bytes(
            session,
            {},
            tenant_id=DEFAULT_TENANT_ID,
            artifact_id=staged.registration.artifact.id,
        )


def _settings(**overrides):
    return Settings(
        _env_file=None,
        admin_auth_enabled=False,
        internal_ingress_hmac_secret=(
            "synthetic-test-secret-that-is-long-enough"
        ),
        **overrides,
    )


def test_artifact_store_configuration_defaults_fail_closed():
    configured = _settings()
    assert configured.artifact_store_enabled is False
    assert (
        configured.artifact_store_root
        == "/var/lib/attention-router/artifacts"
    )
    assert configured.artifact_store_max_bytes == 32 * 1024 * 1024
@pytest.mark.parametrize(
    "max_bytes",
    [0, 1024 * 1024 * 1024 + 1],
)
def test_artifact_store_configuration_bounds(max_bytes):
    with pytest.raises(
        ValidationError,
        match="ARTIFACT_STORE_MAX_BYTES",
    ):
        _settings(artifact_store_max_bytes=max_bytes)


def test_enabled_artifact_store_requires_absolute_root():
    with pytest.raises(
        ValidationError,
        match="ARTIFACT_STORE_ROOT",
    ):
        _settings(
            artifact_store_enabled=True,
            artifact_store_root="relative/artifacts",
        )


def test_enabled_artifact_store_accepts_explicit_absolute_root():
    configured = _settings(
        artifact_store_enabled=True,
        artifact_store_root="/srv/andy/artifacts",
    )
    assert configured.artifact_store_enabled is True
