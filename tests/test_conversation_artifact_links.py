"""Live-only conversation/media association, synthetic source data only.

No photos, OCR text, external model requests, provider credentials or network
activity are required to certify the identity/provenance contract.
"""

from datetime import timedelta
import hashlib

import pytest
from sqlalchemy import func, select

from attention_router.application import services
from attention_router.application.conversation_live_capture import (
    ConversationCaptureConflict,
    attach_canonical_artifact,
    reconcile_live_capture_batch,
)
from attention_router.config import settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.artifact_models import (
    ArtifactReceiptRow,
    ArtifactRow,
    ArtifactUnderstandingRow,
)
from attention_router.infrastructure.models import (
    ConversationMessageRow,
    MemoryIngestionJobRow,
    ResourceRow,
)
from tests.test_conversation_live_capture import (
    STAMP,
    _receipt,
    _transcription,
    inbound,
    messages,
    outgoing,
)


@pytest.fixture(name="capture")
def capture_with_media(session, monkeypatch):
    from tests.test_owner_reply_grace import _install_canary

    _install_canary(session)
    session.commit()
    monkeypatch.setattr(settings, "conversation_capture_enabled", True)
    monkeypatch.setattr(
        settings, "conversation_capture_start_at", STAMP - timedelta(minutes=1),
    )
    monkeypatch.setattr(settings, "conversation_capture_tenant_id", DEFAULT_TENANT_ID)
    monkeypatch.setattr(settings, "persistent_memory_enabled", False)
    monkeypatch.setattr(settings, "memory_ingestion_enabled", False)
    return session


def media_event(uid, kind="image", *, caption="", at=STAMP):
    return inbound(
        uid, caption, at=at,
        metadata={"message_type": kind, "has_media": True},
    )


def stage_synthetic_artifact(
    session,
    event,
    *,
    kind="IMAGE",
    status="READY",
    understanding=True,
    existing_artifact=None,
):
    """Manufacture an already committed canonical Artifact and source receipt."""
    stamp = now_utc()
    source_event = _receipt(session, event)
    assert source_event is not None
    if existing_artifact is None:
        resource = ResourceRow(
            id=new_id(),
            tenant_id=DEFAULT_TENANT_ID,
            resource_type="ARTIFACT",
            canonical_name="synthetic-content-" + new_id(),
            status="ACTIVE",
            metadata_json={},
            created_at=stamp,
            updated_at=stamp,
        )
        session.add(resource)
        session.flush()
        digest = hashlib.sha256(
            ("synthetic-asset-" + event.external_event_id).encode()
        ).hexdigest()
        artifact = ArtifactRow(
            id=new_id(),
            tenant_id=DEFAULT_TENANT_ID,
            resource_id=resource.id,
            content_sha256=digest,
            artifact_kind=kind,
            mime_type=(
                "image/png" if kind == "IMAGE"
                else "application/pdf" if kind == "DOCUMENT"
                else "video/mp4" if kind == "VIDEO"
                else "audio/ogg"
            ),
            size_bytes=40,
            storage_provider="local-fs-v1",
            storage_reference="sha256:" + digest,
            status="AVAILABLE",
            metadata_json={},
            created_at=stamp,
            updated_at=stamp,
        )
        session.add(artifact)
        session.flush()
    else:
        artifact = existing_artifact
    source_account = source_event.payload["metadata"]["source_account"]
    receipt = ArtifactReceiptRow(
        id=new_id(),
        tenant_id=DEFAULT_TENANT_ID,
        artifact_id=artifact.id,
        source_channel="channel.whatsapp",
        source_account=source_account,
        external_receipt_id=f"whatsapp:{source_event.external_event_id}:media",
        sender_actor_id=source_event.payload.get("actor_id"),
        received_at=source_event.received_at,
        metadata_json={"capture_status": "READY"},
        created_at=stamp,
    )
    session.add(receipt)
    if not understanding:
        session.flush()
        return source_event, artifact, receipt, None
    row = ArtifactUnderstandingRow(
        id=new_id(),
        tenant_id=DEFAULT_TENANT_ID,
        artifact_id=artifact.id,
        inbound_event_id=source_event.id,
        status=status,
        content_kind="IMAGE" if kind == "IMAGE" else "DOCUMENT",
        summary_text="DADO PRIVADO DE TESTE" if status == "READY" else None,
        extracted_text="DADO PRIVADO DE TESTE" if status == "READY" else None,
        visual_description="Cenário visual sintético" if status == "READY" else None,
        key_facts_json=["informação privada de teste"] if status == "READY" else [],
        language="pt-BR" if status == "READY" else None,
        text_truncated=False,
        provider="synthetic",
        model="fake",
        prompt_version="fake",
        attempt_count=1,
        created_at=stamp,
        updated_at=stamp,
        completed_at=stamp if status in ("READY", "FAILED") else None,
    )
    session.add(row)
    session.flush()
    return source_event, artifact, receipt, row


def _by_source(session, source_id):
    return session.scalar(select(ConversationMessageRow).where(
        ConversationMessageRow.source_message_id == source_id
    ))


def test_image_caption_then_async_artifact_preserves_one_message(capture):
    event = media_event("photo-caption", "image", caption="Cartão para consultar depois.")
    services.receive_normalized_inbound_event(capture, event)
    capture.commit()
    original = _by_source(capture, event.external_event_id)
    assert original is not None
    identity = (original.id, original.conversation_id, original.sent_at)
    assert original.message_type == "IMAGE"
    assert original.text == "Cartão para consultar depois."
    assert original.metadata_json["attachment_state"] == "PENDING"
    source, artifact, receipt, understanding = stage_synthetic_artifact(
        capture, event,
    )
    capture.commit()
    pending = reconcile_live_capture_batch(capture)
    capture.commit()
    assert pending.linked_artifacts == 1
    archive = _by_source(capture, event.external_event_id)
    assert (archive.id, archive.conversation_id, archive.sent_at) == identity
    assert archive.metadata_json["source_inbound_event_id"] == source.id
    assert archive.metadata_json["attachment_state"] == "READY"
    assert archive.metadata_json["artifact_id"] == artifact.id
    assert archive.metadata_json["artifact_receipt_id"] == receipt.id
    assert archive.metadata_json["artifact_understanding_id"] == understanding.id
    assert archive.metadata_json["artifact_understanding_status"] == "READY"
    assert archive.text == "Cartão para consultar depois."
    assert "DADO PRIVADO DE TESTE" not in archive.text
    assert "DADO PRIVADO DE TESTE" not in str(archive.metadata_json)
    assert reconcile_live_capture_batch(capture).linked_artifacts == 0
    assert len(messages(capture)) == 1
    assert capture.scalar(select(func.count()).select_from(MemoryIngestionJobRow)) == 0


def test_document_pdf_stored_before_archival_is_linked_in_same_recovery(capture, monkeypatch):
    event = media_event("doc-first", "document", caption="Anexo solicitado.")
    monkeypatch.setattr(settings, "conversation_capture_enabled", False)
    services.receive_normalized_inbound_event(capture, event)
    source, artifact, receipt, understanding = stage_synthetic_artifact(
        capture, event, kind="DOCUMENT",
    )
    capture.commit()
    assert _by_source(capture, event.external_event_id) is None
    monkeypatch.setattr(settings, "conversation_capture_enabled", True)
    result = reconcile_live_capture_batch(capture)
    capture.commit()
    assert result.archived == 1
    assert result.linked_artifacts == 1
    archive = _by_source(capture, event.external_event_id)
    assert archive.message_type == "DOCUMENT"
    assert archive.text == "Anexo solicitado."
    assert archive.metadata_json["artifact_id"] == artifact.id
    assert archive.metadata_json["artifact_receipt_id"] == receipt.id
    assert archive.metadata_json["artifact_understanding_id"] == understanding.id
    assert archive.metadata_json["source_inbound_event_id"] == source.id


def test_video_inserts_placeholder_and_links_bytes_without_inventing_semantics(capture):
    event = media_event("video-one", "video")
    services.receive_normalized_inbound_event(capture, event)
    capture.commit()
    row = _by_source(capture, event.external_event_id)
    assert row.message_type == "VIDEO" and row.text is None
    source, artifact, receipt, _ = stage_synthetic_artifact(
        capture, event, kind="VIDEO", understanding=False,
    )
    capture.commit()
    result = reconcile_live_capture_batch(capture)
    capture.commit()
    assert result.linked_artifacts == 1
    assert row.metadata_json["attachment_state"] == "READY"
    assert row.metadata_json["artifact_kind"] == "VIDEO"
    assert row.metadata_json["artifact_understanding_status"] == "NOT_APPLICABLE"
    assert "artifact_understanding_id" not in row.metadata_json
    assert row.metadata_json["artifact_id"] == artifact.id


def test_media_processing_then_delayed_understanding_reconciles_independently(capture):
    event = media_event("photo-late", "image")
    services.receive_normalized_inbound_event(capture, event)
    _source, artifact, _receipt_row, understanding = stage_synthetic_artifact(
        capture, event, kind="IMAGE", status="PENDING",
    )
    capture.commit()
    first = reconcile_live_capture_batch(capture)
    capture.commit()
    assert first.linked_artifacts == 1
    archive = _by_source(capture, event.external_event_id)
    assert archive.metadata_json["attachment_state"] == "READY"
    assert archive.metadata_json["artifact_understanding_status"] == "PENDING"
    understanding.status = "READY"
    understanding.summary_text = "DADO PRIVADO DE TESTE"
    understanding.extracted_text = "DADO PRIVADO DE TESTE"
    understanding.visual_description = "Imagem sintética"
    capture.commit()
    second = reconcile_live_capture_batch(capture)
    capture.commit()
    assert second.linked_artifacts == 1
    assert archive.metadata_json["artifact_understanding_status"] == "READY"
    assert "DADO PRIVADO DE TESTE" not in str(archive.metadata_json)
    assert archive.text is None
    assert archive.metadata_json["artifact_id"] == artifact.id
    assert reconcile_live_capture_batch(capture).linked_artifacts == 0


def test_audio_stt_and_canonical_artifact_have_independent_statuses(capture):
    event = media_event("voice-artifact", "ptt")
    services.receive_normalized_inbound_event(capture, event)
    source, artifact, receipt, _ = stage_synthetic_artifact(
        capture, event, kind="AUDIO", understanding=False,
    )
    _, stt = _transcription(capture, event, text="Voz sintética preservada.")
    from attention_router.application.conversation_live_capture import capture_voice_transcription
    capture_voice_transcription(capture, source, stt)
    capture.commit()
    out = reconcile_live_capture_batch(capture)
    capture.commit()
    row = _by_source(capture, event.external_event_id)
    assert row.message_type == "VOICE"
    assert row.text == "Voz sintética preservada."
    assert row.metadata_json["content_state"] == "TRANSCRIBED"
    assert row.metadata_json["attachment_state"] == "READY"
    assert row.metadata_json["artifact_id"] == artifact.id
    assert row.metadata_json["artifact_receipt_id"] == receipt.id
    assert out.linked_artifacts == 1


def test_failed_photo_capture_remains_observation_without_fake_artifact(capture):
    event = media_event("photo-failed")
    services.receive_normalized_inbound_event(capture, event)
    source = _receipt(capture, event)
    stamp = now_utc()
    failed = ArtifactUnderstandingRow(
        id=new_id(), tenant_id=DEFAULT_TENANT_ID, inbound_event_id=source.id,
        artifact_id=None, status="FAILED", content_kind="IMAGE",
        summary_text=None, extracted_text=None, visual_description=None,
        key_facts_json=[], prompt_version="fake", attempt_count=0,
        text_truncated=False, created_at=stamp, updated_at=stamp,
        completed_at=stamp, error_code="ARTIFACT_MEDIA_FAILED",
    )
    capture.add(failed)
    capture.commit()
    result = reconcile_live_capture_batch(capture)
    capture.commit()
    assert result.linked_artifacts == 1
    row = _by_source(capture, event.external_event_id)
    assert row.metadata_json["attachment_state"] == "FAILED"
    assert row.metadata_json["artifact_understanding_status"] == "FAILED"
    assert row.metadata_json["artifact_understanding_id"] == failed.id
    assert "artifact_id" not in row.metadata_json
    assert row.text is None
    repeat = reconcile_live_capture_batch(capture)
    assert repeat.examined == 0 and repeat.linked_artifacts == 0


def test_missing_media_does_not_starve_later_ready_artifact(capture):
    for uid in ("photo-unavailable", "photo-available"):
        event = media_event(uid)
        services.receive_normalized_inbound_event(capture, event)
        if uid == "photo-available":
            stage_synthetic_artifact(capture, event)
    capture.commit()
    result = reconcile_live_capture_batch(capture, limit=1)
    capture.commit()
    assert result.linked_artifacts == 1
    assert _by_source(capture, "photo-unavailable").metadata_json["attachment_state"] == "PENDING"
    assert _by_source(capture, "photo-available").metadata_json["attachment_state"] == "READY"


def test_same_content_two_events_remain_separate_artifact_receipts(capture):
    first = media_event("photo-duplicate-A", at=STAMP)
    second = media_event("photo-duplicate-B", at=STAMP+timedelta(seconds=5))
    services.receive_normalized_inbound_event(capture, first)
    services.receive_normalized_inbound_event(capture, second)
    _src_a, common, receipt_a, _ = stage_synthetic_artifact(capture, first)
    _src_b, shared, receipt_b, _ = stage_synthetic_artifact(
        capture, second, existing_artifact=common,
    )
    capture.commit()
    result = reconcile_live_capture_batch(capture)
    capture.commit()
    a = _by_source(capture, first.external_event_id)
    b = _by_source(capture, second.external_event_id)
    assert result.linked_artifacts == 2
    assert a.id != b.id
    assert common.id == shared.id
    assert a.metadata_json["artifact_id"] == b.metadata_json["artifact_id"]
    assert a.metadata_json["artifact_receipt_id"] == receipt_a.id
    assert b.metadata_json["artifact_receipt_id"] == receipt_b.id
    assert receipt_a.id != receipt_b.id


def test_mismatched_account_or_sender_is_not_silently_linked(capture):
    event = media_event("wrong-acct-artifact")
    services.receive_normalized_inbound_event(capture, event)
    source, artifact, receipt, understanding = stage_synthetic_artifact(capture, event)
    receipt.source_account = "different-whatsapp-account"
    capture.commit()
    result = reconcile_live_capture_batch(capture)
    capture.commit()
    assert result.linked_artifacts == 0
    row = _by_source(capture, event.external_event_id)
    assert row.metadata_json["attachment_state"] == "PENDING"
    assert "artifact_id" not in row.metadata_json
    # Failed provenance must not be forced into the record.
    with pytest.raises(ConversationCaptureConflict, match="RECEIPT_MISSING"):
        attach_canonical_artifact(capture, source)


def test_media_archive_rejects_false_has_media_and_stale_time(capture):
    old = media_event("photo-old", at=STAMP-timedelta(days=1))
    fake = inbound(
        "image-hint-no-media", "Texto",
        metadata={"message_type": "image", "has_media": False},
    )
    for event in (old, fake):
        services.receive_normalized_inbound_event(capture, event)
    capture.commit()
    assert _by_source(capture, old.external_event_id) is None
    assert _by_source(capture, fake.external_event_id) is None


def test_conflicting_relink_cannot_overwrite_existing_source_identity(capture):
    event = media_event("photo-rewrite")
    services.receive_normalized_inbound_event(capture, event)
    source, _, _, _ = stage_synthetic_artifact(capture, event)
    capture.commit()
    reconcile_live_capture_batch(capture)
    capture.commit()
    row = _by_source(capture, event.external_event_id)
    meta = dict(row.metadata_json)
    meta["artifact_id"] = "foreign-immutable-artifact"
    row.metadata_json = meta
    capture.commit()
    with pytest.raises(ConversationCaptureConflict, match="REFERENCE_CONFLICT"):
        attach_canonical_artifact(capture, source)
    capture.rollback()


def test_ambiguous_owner_media_never_claims_human_authority(capture):
    event = outgoing("outgoing-unknown-photo", "", origin="UNKNOWN_FROM_ME")
    event = event.model_copy(update={
        "metadata": {**event.metadata, "message_type": "image", "has_media": True},
    })
    services.receive_normalized_inbound_event(capture, event)
    capture.commit()
    row = _by_source(capture, event.external_event_id)
    assert row.message_type == "IMAGE" and row.direction == "OUTBOUND"
    assert row.sender_actor_id is None
    assert row.external_sender_key is None
    assert row.metadata_json["speaker_basis"] == "UNRESOLVED"
    assert row.metadata_json["attachment_state"] == "PENDING"

def test_preexisting_live_voice_archived_before_media_link_has_canonical_reference(capture):
    # A VOICE message may have been archived by V0 after the original fixed
    # watermark and prior to introduction of metadata. This is NOT backfill
    # of events older than the watermark.
    event = media_event("live-voice-before-media-feature", "ptt")
    services.receive_normalized_inbound_event(capture, event)
    row = _by_source(capture, event.external_event_id)
    meta = dict(row.metadata_json)
    meta.pop("attachment_state")
    row.metadata_json = meta
    _source, artifact, receipt, _ = stage_synthetic_artifact(
        capture, event, kind="AUDIO", understanding=False,
    )
    capture.commit()
    result = reconcile_live_capture_batch(capture)
    capture.commit()
    assert result.linked_artifacts == 1
    assert row.metadata_json["attachment_state"] == "READY"
    assert row.metadata_json["artifact_id"] == artifact.id
    assert row.metadata_json["artifact_receipt_id"] == receipt.id
    assert row.text is None
    assert reconcile_live_capture_batch(capture).linked_artifacts == 0


def test_failed_media_can_later_attach_when_the_exact_source_is_repaired(capture):
    event = media_event("late-media-repair")
    services.receive_normalized_inbound_event(capture, event)
    source = _receipt(capture, event)
    stamp = now_utc()
    failed = ArtifactUnderstandingRow(
        id=new_id(), tenant_id=DEFAULT_TENANT_ID, inbound_event_id=source.id,
        artifact_id=None, status="FAILED", content_kind="IMAGE",
        summary_text=None, extracted_text=None, visual_description=None,
        key_facts_json=[], prompt_version="fake", attempt_count=0,
        text_truncated=False, created_at=stamp, updated_at=stamp,
        completed_at=stamp, error_code="ARTIFACT_MEDIA_FAILED",
    )
    capture.add(failed)
    capture.commit()
    assert reconcile_live_capture_batch(capture).linked_artifacts == 1
    capture.commit()
    row = _by_source(capture, event.external_event_id)
    assert row.metadata_json["attachment_state"] == "FAILED"

    _src, artifact, receipt, _ = stage_synthetic_artifact(
        capture, event, understanding=False,
    )
    failed.artifact_id = artifact.id
    failed.status = "READY"
    failed.summary_text = "DADO PRIVADO DE TESTE"
    failed.extracted_text = "DADO PRIVADO DE TESTE"
    failed.visual_description = "Foto sintética"
    capture.commit()
    result = reconcile_live_capture_batch(capture)
    capture.commit()
    assert result.linked_artifacts == 1
    assert row.metadata_json["attachment_state"] == "READY"
    assert row.metadata_json["artifact_id"] == artifact.id
    assert row.metadata_json["artifact_receipt_id"] == receipt.id
    assert row.metadata_json["artifact_understanding_status"] == "READY"
    assert "DADO PRIVADO DE TESTE" not in str(row.metadata_json)

def test_failed_semantic_result_is_repaired_if_original_understanding_turns_ready(capture):
    # Evidence remains linked while a previously failed visual model result
    # is later repaired through an independently authorized mechanism.
    event = media_event("semantic-repaired")
    services.receive_normalized_inbound_event(capture, event)
    _source, artifact, _receipt_row, semantic = stage_synthetic_artifact(
        capture, event, status="FAILED",
    )
    capture.commit()
    assert reconcile_live_capture_batch(capture).linked_artifacts == 1
    capture.commit()
    row = _by_source(capture, event.external_event_id)
    assert row.metadata_json["artifact_understanding_status"] == "FAILED"
    assert row.metadata_json["attachment_state"] == "READY"

    semantic.status = "READY"
    semantic.summary_text = "SÍNTESE PRIVADA"
    semantic.extracted_text = "SÍNTESE PRIVADA"
    semantic.visual_description = "Imagem sintética"
    capture.commit()
    repaired = reconcile_live_capture_batch(capture)
    capture.commit()
    assert repaired.linked_artifacts == 1
    assert row.metadata_json["artifact_understanding_status"] == "READY"
    assert row.metadata_json["artifact_id"] == artifact.id
    assert "SÍNTESE PRIVADA" not in str(row.metadata_json)
    assert reconcile_live_capture_batch(capture).linked_artifacts == 0


def test_document_message_with_image_mimetype_preserves_real_content_kind(capture):
    # WhatsApp 'document' describes the source message category. An attached
    # PNG remains an IMAGE Artifact and needs no invented PDF representation.
    event = media_event("document-as-image", "document")
    services.receive_normalized_inbound_event(capture, event)
    _source, artifact, receipt, understanding = stage_synthetic_artifact(
        capture, event, kind="IMAGE",
    )
    capture.commit()
    result = reconcile_live_capture_batch(capture)
    capture.commit()
    row = _by_source(capture, event.external_event_id)
    assert result.linked_artifacts == 1
    assert row.message_type == "DOCUMENT"
    assert row.metadata_json["artifact_kind"] == "IMAGE"
    assert row.metadata_json["artifact_receipt_id"] == receipt.id
    assert row.metadata_json["artifact_understanding_id"] == understanding.id
    assert row.metadata_json["artifact_id"] == artifact.id
    assert row.text is None


def test_bad_media_identity_is_quarantined_without_erasing_archived_message(
    capture, monkeypatch,
):
    # Source event and canonical receipt were observed at different times.
    # A wrong media category is an integrity conflict, not an excuse to lose
    # the message or retry the same contradiction forever.
    wrong = media_event("bad-artifact-kind", "image")
    monkeypatch.setattr(settings, "conversation_capture_enabled", False)
    services.receive_normalized_inbound_event(capture, wrong)
    source, _, _, _ = stage_synthetic_artifact(
        capture, wrong, kind="DOCUMENT", understanding=False,
    )
    capture.commit()
    monkeypatch.setattr(settings, "conversation_capture_enabled", True)
    result = reconcile_live_capture_batch(capture)
    capture.commit()
    row = _by_source(capture, wrong.external_event_id)
    assert result.archived == 1
    assert result.failed >= 1
    assert row is not None and row.message_type == "IMAGE"
    assert row.metadata_json["source_inbound_event_id"] == source.id
    assert row.metadata_json["attachment_state"] == "LINK_CONFLICT"
    assert row.metadata_json["artifact_link_error_code"] == "ARTIFACT_KIND_OR_TENANT_CONFLICT"
    assert "artifact_id" not in row.metadata_json
    assert row.text is None
    assert reconcile_live_capture_batch(capture).examined == 0

    good = media_event("good-artifact-after-conflict", "image")
    services.receive_normalized_inbound_event(capture, good)
    _good_source, artifact, receipt, _ = stage_synthetic_artifact(
        capture, good,
    )
    capture.commit()
    response = reconcile_live_capture_batch(capture)
    capture.commit()
    assert response.linked_artifacts == 1
    linked = _by_source(capture, good.external_event_id)
    assert linked.metadata_json["attachment_state"] == "READY"
    assert linked.metadata_json["artifact_id"] == artifact.id
    assert linked.metadata_json["artifact_receipt_id"] == receipt.id
