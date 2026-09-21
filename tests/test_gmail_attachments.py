from __future__ import annotations

from datetime import UTC, datetime
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from attention_router.application.gmail_attachments import (
    GmailAttachmentIngestor,
)
from attention_router.config import Settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.artifact_models import (
    ArtifactReceiptRow,
    ArtifactRow,
)
from attention_router.infrastructure.artifact_store import LocalArtifactStore
from attention_router.integrations.gmail_connector import (
    GmailAttachmentSummary,
    GmailConnectorConfig,
    GmailConnectorError,
    GmailInboundConnector,
    GmailMessage,
    GmailStagedAttachment,
    IntegrationIngressResponse,
)

NOW = datetime(2026, 9, 21, 19, 0, tzinfo=UTC)


class FakeAttachmentReader:
    def __init__(self, payload: bytes = b"attachment bytes"):
        self.payload = payload
        self.structure_calls = []
        self.download_calls = []

    def read_message_with_attachments(
        self,
        message_id,
        *,
        max_attachments,
        max_mime_depth,
    ):
        self.structure_calls.append(
            (message_id, max_attachments, max_mime_depth)
        )
        return GmailMessage(
            message_id=message_id,
            thread_id="thread-1",
            sender="Sender <sender@example.invalid>",
            to=("owner@example.invalid",),
            cc=(),
            bcc=(),
            subject="with attachment",
            body="",
            email_ts=NOW.isoformat(),
            attachments=(
                GmailAttachmentSummary(
                    attachment_id="att-1",
                    filename="../../report.pdf",
                    mime_type="application/pdf",
                    size_bytes=len(self.payload),
                ),
            ),
            body_observed=False,
            attachments_observed=True,
        )

    def read_attachment(
        self,
        message_id,
        attachment_id,
        *,
        expected_size,
        max_bytes,
    ):
        self.download_calls.append(
            (message_id, attachment_id, expected_size, max_bytes)
        )
        return self.payload

def _settings(**overrides):
    return Settings(
        _env_file=None,
        admin_auth_enabled=False,
        internal_ingress_hmac_secret=(
            "synthetic-test-secret-that-is-long-enough"
        ),
        **overrides,
    )


def _attachment_settings(**overrides):
    values = {
        "client_session_enabled": True,
        "gmail_connect_enabled": True,
        "gmail_product_runner_enabled": True,
        "artifact_store_enabled": True,
        "gmail_attachment_ingestion_enabled": True,
        "google_workspace_oauth_client_id": "synthetic-client",
        "google_workspace_oauth_client_secret": "synthetic-secret",
        "provider_authorization_key_b64url": "A" * 43,
    }
    values.update(overrides)
    return _settings(**values)


def _session_factory(session):
    return sessionmaker(
        bind=session.get_bind(),
        expire_on_commit=False,
        future=True,
    )


def test_attachment_ingestor_commits_stable_artifact_before_event(
    session,
    tmp_path,
):
    settings = _attachment_settings(
        gmail_attachment_max_bytes=1024,
        gmail_attachment_max_total_bytes=2048,
    )
    store = LocalArtifactStore(tmp_path / "artifacts", max_bytes=4096)
    reader = FakeAttachmentReader()
    ingestor = GmailAttachmentIngestor(
        settings=settings,
        session_factory=_session_factory(session),
        store=store,
    )

    first_message, first = ingestor.prepare_message(
        reader,
        tenant_id=DEFAULT_TENANT_ID,
        source_account="sha256:" + "a" * 64,
        message_id="gmail-message-1",
    )
    second_message, second = ingestor.prepare_message(
        reader,
        tenant_id=DEFAULT_TENANT_ID,
        source_account="sha256:" + "a" * 64,
        message_id="gmail-message-1",
    )

    assert first_message == second_message
    assert len(first) == len(second) == 1
    assert first[0].artifact_id == second[0].artifact_id
    assert first[0].external_receipt_id == second[0].external_receipt_id
    assert first[0].external_receipt_id.startswith("gmail-att:")

    session.expire_all()
    assert session.scalar(select(ArtifactRow)).id == first[0].artifact_id
    assert len(session.scalars(select(ArtifactReceiptRow)).all()) == 1
    artifact = session.get(ArtifactRow, first[0].artifact_id)
    assert artifact.original_filename == "../../report.pdf"
    assert "report.pdf" not in str(
        store.path_for_ref(
            DEFAULT_TENANT_ID,
            artifact.storage_reference,
        )
    )


def test_attachment_ingestor_enforces_total_bound_before_staging(
    session,
    tmp_path,
):
    settings = _attachment_settings(
        gmail_attachment_max_bytes=8,
        gmail_attachment_max_total_bytes=8,
    )
    store = LocalArtifactStore(tmp_path / "artifacts", max_bytes=64)
    reader = FakeAttachmentReader(payload=b"123456789")
    ingestor = GmailAttachmentIngestor(
        settings=settings,
        session_factory=_session_factory(session),
        store=store,
    )

    with pytest.raises(
        GmailConnectorError,
        match="GMAIL_ATTACHMENT_SUMMARY_INVALID",
    ):
        ingestor.prepare_message(
            reader,
            tenant_id=DEFAULT_TENANT_ID,
            source_account="mailbox",
            message_id="gmail-message-1",
        )

class RecordingIngress:
    def __init__(self):
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)
        return IntegrationIngressResponse(
            status_code=202,
            body={"status": "accepted"},
        )


class NoopReader:
    def search_message_ids(self, *, query, max_results):
        return ()

    def read_message(self, message_id):
        raise AssertionError("not used")


def test_staged_attachment_enters_event_only_as_artifact_id():
    message = GmailMessage(
        message_id="gmail-message-1",
        thread_id="thread-1",
        sender="Sender <sender@example.invalid>",
        to=("owner@example.invalid",),
        cc=(),
        bcc=(),
        subject="private subject",
        body="",
        email_ts=NOW.isoformat(),
        attachments=(
            GmailAttachmentSummary(
                attachment_id="private-provider-attachment",
                filename="report.pdf",
                mime_type="application/pdf",
                size_bytes=7,
            ),
        ),
        body_observed=False,
        attachments_observed=True,
    )
    staged = (
        GmailStagedAttachment(
            attachment_id="private-provider-attachment",
            artifact_id="artifact-123",
            external_receipt_id="gmail-att-" + "b" * 64,
            content_sha256="c" * 64,
            filename="report.pdf",
            mime_type="application/pdf",
            size_bytes=7,
            storage_provider="local-fs-v1",
            storage_reference="sha256:" + "c" * 64,
        ),
    )

    ingress = RecordingIngress()
    connector = GmailInboundConnector(
        reader=NoopReader(),
        ingress=ingress,
        config=GmailConnectorConfig(
            tenant_id=DEFAULT_TENANT_ID,
            instance_id="gmail-primary",
            account_id="sha256:" + "d" * 64,
            ingress_url="https://router.invalid/ingress",
            ingress_bearer="A" * 43,
        ),
    )

    response = connector.ingest_message(
        message,
        staged_attachments=staged,
        received_at=NOW,
    )

    assert response.status_code == 202
    event = ingress.payloads[0]
    assert event["artifact_ids"] == ["artifact-123"]
    rendered = json.dumps(event, sort_keys=True)
    assert "private-provider-attachment" not in rendered
    assert "local-fs-v1" not in rendered
    assert "sha256:" + "c" * 64 not in rendered
    assert "private subject" not in rendered
    assert event["metadata_sanitized"]["attachment_count"] == 1
    assert event["metadata_sanitized"]["body_observed"] is False


def test_duplicate_content_artifact_id_is_deduplicated_in_event():
    message = GmailMessage(
        message_id="gmail-message-1",
        thread_id="thread-1",
        sender="Sender <sender@example.invalid>",
        to=("owner@example.invalid",),
        cc=(),
        bcc=(),
        subject="",
        body="",
        email_ts=NOW.isoformat(),
        attachments=(
            GmailAttachmentSummary("att-1", "a.bin", "application/octet-stream", 1),
            GmailAttachmentSummary("att-2", "b.bin", "application/octet-stream", 1),
        ),
        body_observed=False,
        attachments_observed=True,
    )

    staged = tuple(
        GmailStagedAttachment(
            attachment_id=f"att-{index}",
            artifact_id="same-artifact",
            external_receipt_id=f"receipt-{index}",
            content_sha256="e" * 64,
            filename=f"{index}.bin",
            mime_type="application/octet-stream",
            size_bytes=1,
            storage_provider="local-fs-v1",
            storage_reference="sha256:" + "e" * 64,
        )
        for index in (1, 2)
    )
    ingress = RecordingIngress()
    connector = GmailInboundConnector(
        reader=NoopReader(),
        ingress=ingress,
        config=GmailConnectorConfig(
            tenant_id=DEFAULT_TENANT_ID,
            instance_id="gmail-primary",
            account_id=None,
            ingress_url="https://router.invalid/ingress",
            ingress_bearer="A" * 43,
        ),
    )

    connector.ingest_message(
        message,
        staged_attachments=staged,
        received_at=NOW,
    )
    assert ingress.payloads[0]["artifact_ids"] == ["same-artifact"]


def test_attachment_configuration_is_default_off_and_bounded():
    configured = _settings()
    assert configured.gmail_attachment_ingestion_enabled is False
    assert configured.gmail_attachment_max_count == 10
    assert configured.gmail_attachment_max_bytes == 25 * 1024 * 1024
    assert configured.gmail_attachment_max_total_bytes == 32 * 1024 * 1024
    assert configured.gmail_attachment_max_mime_depth == 12

@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("gmail_attachment_max_count", 0, "GMAIL_ATTACHMENT_MAX_COUNT"),
        (
            "gmail_attachment_max_mime_depth",
            33,
            "GMAIL_ATTACHMENT_MAX_MIME_DEPTH",
        ),
        (
            "gmail_attachment_max_total_bytes",
            24 * 1024 * 1024,
            "GMAIL_ATTACHMENT_MAX_TOTAL_BYTES",
        ),
    ],
)
def test_attachment_configuration_rejects_invalid_bounds(
    field,
    value,
    reason,
):
    with pytest.raises(ValueError, match=reason):
        _settings(**{field: value})


def test_attachment_feature_requires_runner_and_artifact_store():
    with pytest.raises(
        ValueError,
        match="GMAIL_PRODUCT_RUNNER_ENABLED",
    ):
        _settings(gmail_attachment_ingestion_enabled=True)

    with pytest.raises(
        ValueError,
        match="ARTIFACT_STORE_ENABLED",
    ):
        _attachment_settings(
            artifact_store_enabled=False,
        )
