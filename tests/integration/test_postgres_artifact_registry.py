"""PostgreSQL ordering and replay proof for the canonical Artifact registry."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from attention_router.application.platform.artifacts import (
    ArtifactReceiptInput,
    register_artifact_receipt,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.artifact_models import (
    ArtifactReceiptRow,
    ArtifactRow,
)
from attention_router.infrastructure.models import ResourceRow


pytestmark = pytest.mark.postgres


def _input() -> ArtifactReceiptInput:
    return ArtifactReceiptInput(
        tenant_id=DEFAULT_TENANT_ID,
        content_sha256="a" * 64,
        artifact_kind="DOCUMENT",
        mime_type="application/pdf",
        size_bytes=17,
        storage_provider="local-fs-v1",
        storage_reference="sha256:" + "a" * 64,
        source_channel="channel.whatsapp",
        source_account="whatsapp-local",
        external_receipt_id="whatsapp:postgres-proof:media",
        received_at=datetime(2026, 9, 22, 1, 0, tzinfo=UTC),
        sender_actor_id="synthetic-sender",
        original_filename="proof.pdf",
    )


def test_postgres_artifact_resource_fk_and_replay(Session):
    with Session() as session:
        first = register_artifact_receipt(session, _input())
        session.commit()
        artifact_id = first.artifact.id
        resource_id = first.artifact.resource_id
        receipt_id = first.receipt.id

    with Session() as session:
        artifact = session.get(ArtifactRow, artifact_id)
        resource = session.get(ResourceRow, resource_id)
        receipt = session.get(ArtifactReceiptRow, receipt_id)
        assert artifact is not None
        assert resource is not None
        assert receipt is not None
        assert artifact.resource_id == resource.id
        assert receipt.artifact_id == artifact.id

        replay = register_artifact_receipt(session, _input())
        session.commit()
        assert replay.artifact.id == artifact_id
        assert replay.receipt.id == receipt_id
        assert replay.artifact_created is False
        assert replay.receipt_created is False

        assert session.scalar(select(func.count()).select_from(ResourceRow)) == 1
        assert session.scalar(select(func.count()).select_from(ArtifactRow)) == 1
        assert session.scalar(select(func.count()).select_from(ArtifactReceiptRow)) == 1
