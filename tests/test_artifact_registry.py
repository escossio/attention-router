from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from attention_router.application.platform.artifacts import (
    ArtifactReceiptConflict,
    ArtifactReceiptInput,
    ArtifactRegistrationError,
    register_artifact_receipt,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import now_utc
from attention_router.infrastructure.artifact_models import ArtifactReceiptRow, ArtifactRow
from attention_router.infrastructure.models import ResourceRow, TenantRow


SHA_A = "a" * 64
SHA_B = "b" * 64
RECEIVED_AT = datetime(2026, 9, 10, 15, 30, tzinfo=timezone.utc)


def artifact_input(
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    content_sha256: str = SHA_A,
    source_channel: str = "whatsapp",
    external_receipt_id: str = "synthetic-message-001",
    source_account: str = "default",
) -> ArtifactReceiptInput:
    return ArtifactReceiptInput(
        tenant_id=tenant_id,
        content_sha256=content_sha256,
        artifact_kind="spreadsheet",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=4096,
        storage_provider="object-store",
        storage_reference=f"tenant/{tenant_id}/artifacts/{content_sha256}",
        source_channel=source_channel,
        external_receipt_id=external_receipt_id,
        received_at=RECEIVED_AT,
        source_account=source_account,
        sender_actor_id="synthetic-actor-employee-1",
        original_filename="synthetic-report.xlsx",
        artifact_metadata={"classification": "synthetic-report"},
        receipt_metadata={"synthetic": True},
    )


def test_same_content_from_two_channels_is_one_artifact_with_two_receipts(session):
    first = register_artifact_receipt(session, artifact_input())
    second = register_artifact_receipt(
        session,
        artifact_input(
            source_channel="email",
            source_account="synthetic-mailbox",
            external_receipt_id="synthetic-email-002",
        ),
    )

    assert first.artifact_created is True
    assert first.receipt_created is True
    assert second.artifact_created is False
    assert second.receipt_created is True
    assert first.artifact.id == second.artifact.id
    assert first.receipt.id != second.receipt.id

    artifact_count = session.scalar(select(func.count()).select_from(ArtifactRow))
    receipt_count = session.scalar(select(func.count()).select_from(ArtifactReceiptRow))
    resource_count = session.scalar(
        select(func.count())
        .select_from(ResourceRow)
        .where(ResourceRow.resource_type == "ARTIFACT")
    )
    assert artifact_count == 1
    assert receipt_count == 2
    assert resource_count == 1


def test_same_source_receipt_replay_is_idempotent(session):
    first = register_artifact_receipt(session, artifact_input())
    replay = register_artifact_receipt(session, artifact_input())

    assert replay.artifact_created is False
    assert replay.receipt_created is False
    assert replay.artifact.id == first.artifact.id
    assert replay.receipt.id == first.receipt.id
    assert session.scalar(select(func.count()).select_from(ArtifactReceiptRow)) == 1


def test_same_source_receipt_with_different_content_fails_closed(session):
    register_artifact_receipt(session, artifact_input())

    with pytest.raises(
        ArtifactReceiptConflict,
        match="ARTIFACT_RECEIPT_CONTENT_CONFLICT",
    ):
        register_artifact_receipt(
            session,
            artifact_input(content_sha256=SHA_B),
        )

    assert session.scalar(select(func.count()).select_from(ArtifactRow)) == 1
    assert session.scalar(select(func.count()).select_from(ArtifactReceiptRow)) == 1


def test_identical_content_is_not_deduplicated_across_tenants(session):
    stamp = now_utc()
    second_tenant_id = "00000000-0000-4000-8000-000000000099"
    session.add(
        TenantRow(
            id=second_tenant_id,
            slug="synthetic_second_tenant",
            name="Synthetic Second Tenant",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()

    first = register_artifact_receipt(session, artifact_input())
    second = register_artifact_receipt(
        session,
        artifact_input(
            tenant_id=second_tenant_id,
            external_receipt_id="synthetic-message-tenant-2",
        ),
    )

    assert first.artifact.content_sha256 == second.artifact.content_sha256
    assert first.artifact.id != second.artifact.id
    assert first.artifact.resource_id != second.artifact.resource_id
    assert session.scalar(select(func.count()).select_from(ArtifactRow)) == 2


def test_registration_requires_canonical_hash_and_timezone(session):
    with pytest.raises(ArtifactRegistrationError, match="ARTIFACT_SHA256_INVALID"):
        register_artifact_receipt(
            session,
            artifact_input(content_sha256="not-a-sha256"),
        )

    naive = artifact_input()
    with pytest.raises(
        ArtifactRegistrationError,
        match="ARTIFACT_RECEIVED_AT_TIMEZONE_REQUIRED",
    ):
        register_artifact_receipt(
            session,
            ArtifactReceiptInput(
                tenant_id=naive.tenant_id,
                content_sha256=naive.content_sha256,
                artifact_kind=naive.artifact_kind,
                mime_type=naive.mime_type,
                size_bytes=naive.size_bytes,
                storage_provider=naive.storage_provider,
                storage_reference=naive.storage_reference,
                source_channel=naive.source_channel,
                external_receipt_id="synthetic-naive-time",
                received_at=datetime(2026, 9, 10, 15, 30),
            ),
        )
