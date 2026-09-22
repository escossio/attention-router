from __future__ import annotations

from pydantic import ValidationError
import pytest
from sqlalchemy import select

from attention_router.application.artifact_understanding import (
    ArtifactUnderstandingProviderResult,
    ArtifactUnderstandingResult,
    OpenAIArtifactUnderstandingProvider,
    artifact_decision_readiness,
    effective_artifact_text,
    process_artifact_understandings,
    register_artifact_understanding_notification,
)
from attention_router.application.voice_media import MediaReadyNotification
from attention_router.config import Settings, settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure import human_identity_models  # noqa: F401
from attention_router.infrastructure.artifact_models import (
    ArtifactUnderstandingRow,
)
from attention_router.infrastructure.media_store import MediaStore
from attention_router.infrastructure.models import InboundEventRow, QueueRow
from test_internal_ingress import _post_media, client, payload, post


class FakeProvider:
    provider_name = "openai"
    model = "gpt-5.6-sol"

    def __init__(self):
        self.calls = 0

    def analyze(self, data, *, mime_type, filename):
        self.calls += 1
        assert data
        assert mime_type
        return ArtifactUnderstandingProviderResult(
            analysis=ArtifactUnderstandingResult(
                summary="Documento de teste com uma cobrança.",
                extracted_text="TOTAL R$ 42,00",
                visual_description=(
                    "Uma página branca com texto preto."
                    if mime_type.startswith("image/")
                    else ""
                ),
                key_facts=["valor total: R$ 42,00"],
                language="pt-BR",
                text_truncated=False,
            ),
            provider=self.provider_name,
            model=self.model,
            request_reference="fake-request-1",
        )


class FakeResponse:
    status = "completed"
    id = "resp_synthetic"

    def __init__(self, result):
        self.output_text = result.model_dump_json()


class RecordingResponses:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self.result)


class RecordingClient:
    def __init__(self, result):
        self.responses = RecordingResponses(result)


def _analysis_result():
    return ArtifactUnderstandingResult(
        summary="Resumo sintético",
        extracted_text="TEXTO VISÍVEL",
        visual_description="Descrição visual sintética",
        key_facts=["fato 1"],
        language="pt-BR",
        text_truncated=False,
    )


def _enable(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "artifact_store_enabled", True)
    monkeypatch.setattr(
        settings,
        "artifact_store_root",
        str(tmp_path / "artifacts"),
    )
    monkeypatch.setattr(settings, "artifact_store_max_bytes", 1024 * 1024)
    monkeypatch.setattr(settings, "whatsapp_artifact_ingestion_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_artifact_max_bytes", 1024 * 1024)
    monkeypatch.setattr(settings, "whatsapp_media_root", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "whatsapp_media_max_bytes", 1024 * 1024)
    monkeypatch.setattr(settings, "artifact_understanding_enabled", True)
    monkeypatch.setattr(settings, "artifact_understanding_provider", "openai")
    monkeypatch.setattr(settings, "artifact_understanding_model", "gpt-5.6-sol")
    monkeypatch.setattr(settings, "artifact_understanding_max_bytes", 1024 * 1024)
    monkeypatch.setattr(settings, "artifact_understanding_batch_size", 10)


def _post_artifact_media(
    c,
    tmp_path,
    *,
    event_id,
    data=b"synthetic document bytes",
    mime_type="application/pdf",
    media_kind="document",
    filename="report.pdf",
):
    staging = MediaStore(tmp_path / "media", 1024 * 1024)
    reference, digest, size, _path = staging.put_opaque_bytes(
        data,
        max_bytes=1024 * 1024,
    )
    return _post_media(
        c,
        {
            "tenant_id": DEFAULT_TENANT_ID,
            "source": "wwebjs",
            "external_event_id": event_id,
            "media_ref": reference,
            "content_sha256": digest,
            "mime_type": mime_type,
            "size_bytes": size,
            "media_kind": media_kind,
            "original_filename": filename,
            "capture_status": "READY",
        },
    )


def test_openai_provider_uses_image_input_without_file_parser(monkeypatch):
    monkeypatch.setattr(settings, "artifact_understanding_max_bytes", 1024)
    recording = RecordingClient(_analysis_result())
    provider = OpenAIArtifactUnderstandingProvider(
        api_key="synthetic",
        model="gpt-5.6-sol",
        client_factory=lambda **_kwargs: recording,
    )

    result = provider.analyze(
        b"synthetic-image",
        mime_type="image/png",
        filename="../../photo.png",
    )

    assert result.analysis.summary == "Resumo sintético"
    request = recording.responses.calls[0]
    content = request["input"][1]["content"]
    assert content[1]["type"] == "input_image"
    assert content[1]["detail"] == "high"
    assert content[1]["image_url"].startswith("data:image/png;base64,")
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True


def test_openai_provider_uses_pdf_file_input_and_sanitized_filename(monkeypatch):
    monkeypatch.setattr(settings, "artifact_understanding_max_bytes", 1024)
    recording = RecordingClient(_analysis_result())
    provider = OpenAIArtifactUnderstandingProvider(
        api_key="synthetic",
        model="gpt-5.6-sol",
        client_factory=lambda **_kwargs: recording,
    )

    provider.analyze(
        b"%PDF-synthetic",
        mime_type="application/pdf",
        filename="../../private/report.pdf",
    )

    content = recording.responses.calls[0]["input"][1]["content"]
    assert content[1]["type"] == "input_file"
    assert content[1]["filename"] == "report.pdf"
    assert ".." not in content[1]["filename"]
    assert content[1]["file_data"].startswith(
        "data:application/pdf;base64,"
    )
    assert content[1]["detail"] == "high"
    assert "%PDF" not in content[1]["file_data"]


def test_whatsapp_pdf_waits_for_understanding_then_releases_queue(
    session,
    monkeypatch,
    tmp_path,
):
    _enable(monkeypatch, tmp_path)
    c = client(session, monkeypatch)
    event_id = "wamid.understanding.pdf"
    inbound = payload(event_id, "")
    inbound["message_type"] = "document"
    inbound["has_media"] = True
    inbound["metadata"] = {"source_account": "whatsapp-local"}
    assert post(c, inbound).status_code == 200

    response = _post_artifact_media(c, tmp_path, event_id=event_id)
    assert response.status_code == 200
    body = response.json()
    assert body["canonical_artifact_id"]
    assert body["artifact_understanding_id"]

    session.expire_all()
    event = session.scalar(
        select(InboundEventRow).where(
            InboundEventRow.external_event_id == event_id
        )
    )
    row = session.get(
        ArtifactUnderstandingRow,
        body["artifact_understanding_id"],
    )
    queue = session.get(QueueRow, f"decision:{event.id}")
    assert row.status == "PENDING"
    assert row.artifact_id == body["canonical_artifact_id"]
    assert queue.status == "WAITING_ARTIFACT_UNDERSTANDING"
    assert artifact_decision_readiness(session, event)[0] == "WAITING"

    fake = FakeProvider()
    assert process_artifact_understandings(
        session,
        "test-worker",
        provider=fake,
    ) == 1

    session.expire_all()
    row = session.get(ArtifactUnderstandingRow, row.id)
    queue = session.get(QueueRow, queue.id)
    assert row.status == "READY"
    assert row.summary_text.startswith("Documento de teste")
    assert row.extracted_text == "TOTAL R$ 42,00"
    assert row.provider == "openai"
    assert queue.status == "PENDING"
    assert artifact_decision_readiness(session, event)[0] == "READY"

    effective = effective_artifact_text(session, event, "")
    assert "ANEXO_NAO_CONFIAVEL" in effective
    assert "TOTAL R$ 42,00" in effective
    assert "R$ 42,00" in effective


def test_same_artifact_reuses_ready_understanding_without_second_provider_call(
    session,
    monkeypatch,
    tmp_path,
):
    _enable(monkeypatch, tmp_path)
    c = client(session, monkeypatch)
    data = b"same pdf bytes for two messages"
    for event_id in ("wamid.reuse.1", "wamid.reuse.2"):
        inbound = payload(event_id, "")
        inbound["message_type"] = "document"
        inbound["has_media"] = True
        inbound["metadata"] = {"source_account": "whatsapp-local"}
        assert post(c, inbound).status_code == 200
        assert _post_artifact_media(
            c,
            tmp_path,
            event_id=event_id,
            data=data,
        ).status_code == 200

    session.expire_all()
    rows = list(
        session.scalars(
            select(ArtifactUnderstandingRow).order_by(
                ArtifactUnderstandingRow.created_at
            )
        ).all()
    )
    assert len(rows) == 2
    assert rows[0].artifact_id == rows[1].artifact_id

    fake = FakeProvider()
    assert process_artifact_understandings(
        session,
        "test-worker",
        provider=fake,
    ) == 2
    assert fake.calls == 1

    session.expire_all()
    rows = list(
        session.scalars(
            select(ArtifactUnderstandingRow).order_by(
                ArtifactUnderstandingRow.created_at
            )
        ).all()
    )
    assert [row.status for row in rows] == ["READY", "READY"]
    assert rows[0].summary_text == rows[1].summary_text


def test_photo_understanding_persists_visual_description(
    session,
    monkeypatch,
    tmp_path,
):
    _enable(monkeypatch, tmp_path)
    c = client(session, monkeypatch)
    event_id = "wamid.understanding.image"
    inbound = payload(event_id, "")
    inbound["message_type"] = "image"
    inbound["has_media"] = True
    inbound["metadata"] = {"source_account": "whatsapp-local"}
    assert post(c, inbound).status_code == 200
    assert _post_artifact_media(
        c,
        tmp_path,
        event_id=event_id,
        data=b"synthetic png bytes",
        mime_type="image/png",
        media_kind="image",
        filename="photo.png",
    ).status_code == 200

    fake = FakeProvider()
    process_artifact_understandings(
        session,
        "test-worker",
        provider=fake,
    )
    row = session.scalar(select(ArtifactUnderstandingRow))
    assert row.status == "READY"
    assert "página branca" in row.visual_description


def _settings(**overrides):
    return Settings(
        _env_file=None,
        admin_auth_enabled=False,
        internal_ingress_hmac_secret=(
            "synthetic-test-secret-that-is-long-enough"
        ),
        **overrides,
    )


def test_artifact_understanding_configuration_defaults_off():
    configured = _settings()
    assert configured.artifact_understanding_enabled is False
    assert configured.artifact_understanding_provider == "openai"
    assert configured.artifact_understanding_model == "gpt-5.6-sol"
    assert configured.artifact_understanding_max_bytes == 20 * 1024 * 1024


def test_artifact_understanding_enabled_requires_store_and_openai_key():
    with pytest.raises(ValidationError, match="ARTIFACT_STORE_ENABLED"):
        _settings(
            artifact_understanding_enabled=True,
            openai_api_key="synthetic",
        )

    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        _settings(
            artifact_store_enabled=True,
            artifact_understanding_enabled=True,
        )

    configured = _settings(
        artifact_store_enabled=True,
        artifact_understanding_enabled=True,
        openai_api_key="synthetic",
    )
    assert configured.artifact_understanding_enabled is True


def test_decision_worker_waits_before_media_notification(
    session,
    monkeypatch,
    tmp_path,
):
    from attention_router.infrastructure.worker import process_agent_decisions

    _enable(monkeypatch, tmp_path)
    c = client(session, monkeypatch)
    event_id = "wamid.understanding.race"
    inbound = payload(event_id, "")
    inbound["message_type"] = "image"
    inbound["has_media"] = True
    inbound["metadata"] = {"source_account": "whatsapp-local"}
    assert post(c, inbound).status_code == 200

    session.expire_all()
    event = session.scalar(
        select(InboundEventRow).where(
            InboundEventRow.external_event_id == event_id
        )
    )
    queue = session.get(QueueRow, f"decision:{event.id}")
    assert queue.status in {"PENDING", "pending"}

    assert process_agent_decisions(
        session,
        "test-worker",
        limit=10,
    ) == 0
    session.flush()

    queue = session.get(QueueRow, queue.id)
    assert queue.status == "WAITING_ARTIFACT_UNDERSTANDING"


def test_failed_media_notification_cancels_existing_understanding_wait(
    session,
    monkeypatch,
    tmp_path,
):
    from attention_router.infrastructure.worker import process_agent_decisions

    _enable(monkeypatch, tmp_path)
    c = client(session, monkeypatch)
    event_id = "wamid.understanding.failed-after-wait"
    inbound = payload(event_id, "")
    inbound["message_type"] = "image"
    inbound["has_media"] = True
    inbound["metadata"] = {"source_account": "whatsapp-local"}
    assert post(c, inbound).status_code == 200

    session.expire_all()
    event = session.scalar(
        select(InboundEventRow).where(
            InboundEventRow.external_event_id == event_id
        )
    )
    queue = session.get(QueueRow, f"decision:{event.id}")
    assert process_agent_decisions(session, "test-worker", limit=10) == 0
    session.flush()
    assert queue.status == "WAITING_ARTIFACT_UNDERSTANDING"

    row = register_artifact_understanding_notification(
        session,
        MediaReadyNotification(
            tenant_id=DEFAULT_TENANT_ID,
            source="wwebjs",
            external_event_id=event_id,
            media_kind="image",
            capture_status="FAILED",
        ),
        artifact_id=None,
    )
    session.flush()

    assert row is not None
    assert row.status == "FAILED"
    assert row.error_code == "ARTIFACT_MEDIA_FAILED"
    assert queue.status == "CANCELED"
    assert queue.processed_at is not None
