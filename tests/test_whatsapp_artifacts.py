from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from attention_router.adapters.internal_ingress import InternalIngressAdapter
from attention_router.application.voice_media import MediaReadyNotification
from attention_router.application.whatsapp_artifacts import (
    WhatsAppArtifactRetryable,
    stage_whatsapp_artifact_notification,
)
from attention_router.config import Settings, settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.artifact_models import (
    ArtifactReceiptRow,
    ArtifactRow,
)
from attention_router.infrastructure.artifact_store import LocalArtifactStore
from attention_router.infrastructure import human_identity_models  # noqa: F401
from attention_router.infrastructure.media_store import MediaStore
from attention_router.infrastructure.models import TenantRow
from attention_router.infrastructure.repository import create_inbound_event


RECEIVED_AT = datetime(2026, 9, 21, 22, 0, tzinfo=UTC)
def _event(
    session,
    *,
    event_id: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    source_account: str = "whatsapp-local",
    message_type: str = "image",
):
    return create_inbound_event(
        session,
        source="wwebjs",
        external_event_id=event_id,
        event_type="message",
        payload={
            "actor_id": "sender@example.invalid",
            "channel": "whatsapp",
            "content": "",
            "metadata": {
                "source_account": source_account,
                "message_type": message_type,
                "has_media": True,
            },
        },
        correlation_id=new_id(),
        tenant_id=tenant_id,
        received_at=RECEIVED_AT,
    )
def _notification(
    *,
    event_id: str,
    reference: str,
    digest: str,
    size: int,
    tenant_id: str = DEFAULT_TENANT_ID,
    mime_type: str = "image/png",
    media_kind: str = "image",
    filename: str | None = "../../photo.png",
):
    return MediaReadyNotification(
        tenant_id=tenant_id,
        source="wwebjs",
        external_event_id=event_id,
        media_ref=reference,
        content_sha256=digest,
        mime_type=mime_type,
        size_bytes=size,
        media_kind=media_kind,
        original_filename=filename,
        capture_status="READY",
    )


def _enable(monkeypatch, tmp_path, *, max_bytes=1024):
    monkeypatch.setattr(settings, "artifact_store_enabled", True)
    monkeypatch.setattr(settings, "artifact_store_root", str(tmp_path / "artifacts"))
    monkeypatch.setattr(settings, "artifact_store_max_bytes", max_bytes)
    monkeypatch.setattr(settings, "whatsapp_artifact_ingestion_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_artifact_max_bytes", max_bytes)
    monkeypatch.setattr(settings, "whatsapp_media_root", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "whatsapp_media_max_bytes", max_bytes)


def _staging(tmp_path, data: bytes):
    store = MediaStore(tmp_path / "media", max(1024, len(data)))
    return (*store.put_opaque_bytes(data, max_bytes=max(1024, len(data))), store)


def _second_tenant(session) -> str:
    tenant_id = "00000000-0000-4000-8000-000000000099"
    stamp = now_utc()
    session.add(
        TenantRow(
            id=tenant_id,
            slug="whatsapp-artifact-second",
            name="WhatsApp Artifact Second",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()
    return tenant_id
def test_whatsapp_image_stages_canonical_artifact(
    session,
    monkeypatch,
    tmp_path,
):
    _enable(monkeypatch, tmp_path)
    _event(session, event_id="wamid.image.1")
    reference, digest, size, _path, staging = _staging(
        tmp_path,
        b"synthetic png bytes",
    )

    result = stage_whatsapp_artifact_notification(
        session,
        _notification(
            event_id="wamid.image.1",
            reference=reference,
            digest=digest,
            size=size,
        ),
        media_store=staging,
    )

    assert result is not None
    artifact = result.registration.artifact
    receipt = result.registration.receipt
    assert artifact.tenant_id == DEFAULT_TENANT_ID
    assert artifact.artifact_kind == "IMAGE"
    assert artifact.mime_type == "image/png"
    assert artifact.original_filename == "../../photo.png"
    assert receipt.tenant_id == DEFAULT_TENANT_ID
    assert receipt.source_channel == "channel.whatsapp"
    assert receipt.source_account == "whatsapp-local"
    assert receipt.external_receipt_id == "whatsapp:wamid.image.1:media"
    assert receipt.artifact_id == artifact.id
    assert receipt.sender_actor_id == "sender@example.invalid"

    canonical = LocalArtifactStore(
        tmp_path / "artifacts",
        max_bytes=1024,
    )
    stored_path = canonical.path_for_ref(
        DEFAULT_TENANT_ID,
        artifact.storage_reference,
    )
    assert stored_path.is_file()
    assert "photo.png" not in str(stored_path)


def test_whatsapp_artifact_replay_is_idempotent(
    session,
    monkeypatch,
    tmp_path,
):
    _enable(monkeypatch, tmp_path)
    _event(session, event_id="wamid.replay")
    reference, digest, size, _path, staging = _staging(
        tmp_path,
        b"replayed bytes",
    )
    notification = _notification(
        event_id="wamid.replay",
        reference=reference,
        digest=digest,
        size=size,
        mime_type="application/pdf",
        media_kind="document",
        filename="report.pdf",
    )

    first = stage_whatsapp_artifact_notification(
        session,
        notification,
        media_store=staging,
    )
    second = stage_whatsapp_artifact_notification(
        session,
        notification,
        media_store=staging,
    )

    assert first.registration.artifact.id == second.registration.artifact.id
    assert first.registration.receipt.id == second.registration.receipt.id
    assert second.registration.artifact_created is False
    assert second.registration.receipt_created is False
    assert len(session.scalars(select(ArtifactRow)).all()) == 1
    assert len(session.scalars(select(ArtifactReceiptRow)).all()) == 1


def test_same_bytes_two_messages_share_artifact_keep_two_receipts(
    session,
    monkeypatch,
    tmp_path,
):
    _enable(monkeypatch, tmp_path)
    _event(session, event_id="wamid.same.1")
    _event(session, event_id="wamid.same.2")
    reference, digest, size, _path, staging = _staging(
        tmp_path,
        b"same attachment bytes",
    )

    results = [
        stage_whatsapp_artifact_notification(
            session,
            _notification(
                event_id=event_id,
                reference=reference,
                digest=digest,
                size=size,
                mime_type="application/pdf",
                media_kind="document",
                filename=f"{event_id}.pdf",
            ),
            media_store=staging,
        )
        for event_id in ("wamid.same.1", "wamid.same.2")
    ]

    assert results[0].registration.artifact.id == results[1].registration.artifact.id
    assert results[0].registration.receipt.id != results[1].registration.receipt.id
    assert len(session.scalars(select(ArtifactRow)).all()) == 1
    assert len(session.scalars(select(ArtifactReceiptRow)).all()) == 2


def test_notification_cannot_cross_tenant_boundary(
    session,
    monkeypatch,
    tmp_path,
):
    _enable(monkeypatch, tmp_path)
    _event(session, event_id="wamid.tenant")
    other = _second_tenant(session)
    reference, digest, size, _path, staging = _staging(
        tmp_path,
        b"tenant scoped",
    )
    with pytest.raises(
        WhatsAppArtifactRetryable,
        match="INBOUND_EVENT_NOT_COMMITTED",
    ):
        stage_whatsapp_artifact_notification(
            session,
            _notification(
                tenant_id=other,
                event_id="wamid.tenant",
                reference=reference,
                digest=digest,
                size=size,
            ),
            media_store=staging,
        )

    _event(
        session,
        event_id="wamid.tenant",
        tenant_id=other,
        source_account="whatsapp-second",
    )
    first = stage_whatsapp_artifact_notification(
        session,
        _notification(
            event_id="wamid.tenant",
            reference=reference,
            digest=digest,
            size=size,
        ),
        media_store=staging,
    )
    second = stage_whatsapp_artifact_notification(
        session,
        _notification(
            tenant_id=other,
            event_id="wamid.tenant",
            reference=reference,
            digest=digest,
            size=size,
        ),
        media_store=staging,
    )
    assert first.registration.artifact.id != second.registration.artifact.id
    assert first.registration.artifact.tenant_id == DEFAULT_TENANT_ID
    assert second.registration.artifact.tenant_id == other


def test_non_ready_or_disabled_never_fabricates_artifact(
    session,
    monkeypatch,
    tmp_path,
):
    _event(session, event_id="wamid.disabled")
    reference, digest, size, _path, staging = _staging(
        tmp_path,
        b"disabled bytes",
    )
    notification = _notification(
        event_id="wamid.disabled",
        reference=reference,
        digest=digest,
        size=size,
    )
    assert stage_whatsapp_artifact_notification(
        session,
        notification,
        media_store=staging,
    ) is None
    assert session.scalar(select(ArtifactRow)) is None

    _enable(monkeypatch, tmp_path)
    failed = notification.model_copy(
        update={
            "media_ref": None,
            "content_sha256": None,
            "mime_type": None,
            "size_bytes": None,
            "capture_status": "FAILED",
        }
    )
    assert stage_whatsapp_artifact_notification(
        session,
        failed,
        media_store=staging,
    ) is None
    assert session.scalar(select(ArtifactRow)) is None


def _settings(**overrides):
    return Settings(
        _env_file=None,
        admin_auth_enabled=False,
        internal_ingress_hmac_secret=(
            "synthetic-test-secret-that-is-long-enough"
        ),
        **overrides,
    )


def test_whatsapp_artifact_configuration_defaults_off():
    configured = _settings()
    assert configured.whatsapp_artifact_ingestion_enabled is False
    assert configured.whatsapp_artifact_max_bytes == 32 * 1024 * 1024


def test_whatsapp_artifact_configuration_requires_store_and_bounds():
    with pytest.raises(
        ValidationError,
        match="ARTIFACT_STORE_ENABLED",
    ):
        _settings(whatsapp_artifact_ingestion_enabled=True)

    with pytest.raises(
        ValidationError,
        match="WHATSAPP_ARTIFACT_MAX_BYTES",
    ):
        _settings(whatsapp_artifact_max_bytes=0)
    with pytest.raises(
        ValidationError,
        match="WHATSAPP_ARTIFACT_MAX_BYTES",
    ):
        _settings(
            artifact_store_enabled=True,
            artifact_store_max_bytes=1024,
            whatsapp_artifact_ingestion_enabled=True,
            whatsapp_artifact_max_bytes=2048,
        )

def test_media_only_internal_message_is_valid_but_empty_non_media_is_not():
    raw = {
        "source": "wwebjs",
        "external_event_id": "wamid.media.only",
        "event_type": "message",
        "external_actor_id": "sender@example.invalid",
        "channel": "whatsapp",
        "message_type": "document",
        "content": "",
        "has_media": True,
        "metadata": {"source_account": "whatsapp-local"},
    }
    event = InternalIngressAdapter().normalize(raw)
    assert event.content == ""
    assert event.metadata["has_media"] is True

    raw["external_event_id"] = "wamid.empty"
    raw["has_media"] = False
    with pytest.raises(ValueError, match="INBOUND_CONTENT_REQUIRED"):
        InternalIngressAdapter().normalize(raw)
