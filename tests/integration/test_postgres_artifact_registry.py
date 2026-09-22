from datetime import datetime, timezone

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


def test_postgres_resource_exists_before_artifact_fk(Session):
    with Session() as session:
        result = register_artifact_receipt(
            session,
            ArtifactReceiptInput(
                tenant_id=DEFAULT_TENANT_ID,
                content_sha256="f" * 64,
                artifact_kind="DOCUMENT",
                mime_type="application/pdf",
                size_bytes=18761,
                storage_provider="local-fs-v1",
                storage_reference="sha256:" + "f" * 64,
                source_channel="channel.whatsapp",
                external_receipt_id="whatsapp:synthetic-live-proof:media",
                received_at=datetime(2026, 9, 22, 3, 55, tzinfo=timezone.utc),
                source_account="whatsapp-local",
                sender_actor_id="synthetic-sender",
                original_filename="synthetic.pdf",
            ),
        )
        session.commit()

        artifact = session.get(ArtifactRow, result.artifact.id)
        resource = session.get(ResourceRow, result.artifact.resource_id)
        receipt = session.get(ArtifactReceiptRow, result.receipt.id)

        assert resource is not None
        assert artifact is not None
        assert receipt is not None
        assert artifact.resource_id == resource.id
        assert receipt.artifact_id == artifact.id
        assert resource.tenant_id == DEFAULT_TENANT_ID
        assert artifact.tenant_id == DEFAULT_TENANT_ID
