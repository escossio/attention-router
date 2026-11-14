"""Outbound account media proof: no human attribution and no command authority.

All fixtures are synthetic and contained in the disposable test database.
"""

from copy import deepcopy

import pytest
from sqlalchemy import func, select

from attention_router.application.whatsapp_artifacts import (
    stage_whatsapp_artifact_notification,
)
from attention_router.config import settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.artifact_models import (
    ArtifactReceiptRow, ArtifactRow,
)
from attention_router.infrastructure.models import (
    ExecutionIntentRow, MemoryClaimRow, OutboxMessageRow,
)
from tests.test_whatsapp_artifacts import (
    _enable, _event, _notification, _staging,
)

ACCOUNT_BASIS = "VERIFIED_ACCOUNT_UNATTRIBUTED"


def _outbound(session, *, event_id="wamid.synthetic.account.1", kind="image"):
    event = _event(session, event_id=event_id, message_type=kind)
    payload = deepcopy(event.payload)
    payload.update({
        "event_origin": "UNKNOWN_FROM_ME",
        "owner_authenticated": False,
        "actor_id": "peer-do-not-attribute@example.invalid",
    })
    payload["metadata"].update({
        "from_me": True,
        "from_me_classification": "UNKNOWN_FROM_ME",
        "final_from_me_classification": "UNKNOWN_FROM_ME",
        "from_me_classification_reason": "UNMATCHED_MEDIA_FROM_ME",
        "account_outbound_media_capture_basis": ACCOUNT_BASIS,
        "has_media": True,
        "conversation_state": "READY",
        "conversation_key": "wwebjs:peer@lid",
        "peer_id_kind": "lid",
        "outbound_provenance": None,
        "owner_self_chat": False,
        "authenticated_owner_self_chat_target": False,
        "is_group": False,
    })
    event.payload = payload
    session.flush()
    return event


def _ready_notification(session, tmp_path, *, event_id="wamid.synthetic.account.1",
                        mime_type="image/png", media_kind="image"):
    ref, digest, size, _path, staging = _staging(
        tmp_path, b"synthetic opaque photo; not a real card",
    )
    notification = _notification(
        event_id=event_id, reference=ref, digest=digest,
        size=size, mime_type=mime_type, media_kind=media_kind,
    )
    return notification, staging


def test_unknown_from_me_image_stages_without_attributing_the_peer(
    session, monkeypatch, tmp_path,
):
    _enable(monkeypatch, tmp_path)
    event = _outbound(session)
    notification, staging = _ready_notification(session, tmp_path)
    first = stage_whatsapp_artifact_notification(
        session, notification, media_store=staging,
    )
    second = stage_whatsapp_artifact_notification(
        session, notification, media_store=staging,
    )
    assert first is not None
    assert second is not None
    receipt = first.registration.receipt
    assert first.registration.artifact.artifact_kind == "IMAGE"
    assert receipt.tenant_id == event.tenant_id
    assert receipt.source_account == event.payload["metadata"]["source_account"]
    assert receipt.external_receipt_id == f"whatsapp:{event.external_event_id}:media"
    assert receipt.sender_actor_id is None
    assert second.registration.receipt.id == receipt.id
    assert session.scalar(select(func.count()).select_from(ArtifactRow)) == 1
    assert session.scalar(select(func.count()).select_from(ArtifactReceiptRow)) == 1
    assert session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 0
    assert session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 0
    assert session.scalar(select(func.count()).select_from(MemoryClaimRow)) == 0


@pytest.mark.parametrize(
    ("where", "value"),
    [
        ("event_origin", "ROUTER_AUTOMATED_OUTBOUND_OBSERVED"),
        ("event_origin", "OWNER_COMMAND"),
        ("owner_authenticated", True),
        ("from_me_classification", "ROUTER_AUTOMATED_OUTBOUND_OBSERVED"),
        ("final_from_me_classification", "OWNER_MANUAL_OUTBOUND_OBSERVED"),
        ("from_me_classification_reason", "OUTBOUND_PROVENANCE_UNAVAILABLE"),
        ("from_me_classification_reason", "MULTIPLE_OUTBOUND_PROVENANCE_MATCHES"),
        ("from_me_classification_reason", "MEDIA_REFERENCE_PENDING"),
        ("from_me_classification_reason", None),
        ("account_outbound_media_capture_basis", None),
        ("account_outbound_media_capture_basis", "AUTOMATED"),
        ("conversation_state", "AMBIGUOUS"),
        ("peer_id_kind", "group"),
        ("outbound_provenance", {"outbox_id": "fake"}),
        ("owner_self_chat", True),
        ("authenticated_owner_self_chat_target", True),
        ("is_group", True),
        ("has_media", False),
        ("message_type", "ptt"),
        ("from_me", False),
        ("conversation_key", None),
        ("conversation_key", "unknown:peer@lid"),
    ],
)
def test_untrusted_account_media_cannot_enter_artifact(
    session, monkeypatch, tmp_path, where, value,
):
    _enable(monkeypatch, tmp_path)
    event = _outbound(session)
    payload = deepcopy(event.payload)
    if where in {"event_origin", "owner_authenticated"}:
        payload[where] = value
    else:
        payload["metadata"][where] = value
    event.payload = payload
    session.flush()
    notification, staging = _ready_notification(session, tmp_path)
    with pytest.raises(ValueError, match="OUTBOUND_"):
        stage_whatsapp_artifact_notification(
            session, notification, media_store=staging,
        )
    assert session.scalar(select(ArtifactRow)) is None
    assert session.scalar(select(ArtifactReceiptRow)) is None


def test_account_image_cannot_impersonate_a_pdf(session, monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    _outbound(session)
    notification, staging = _ready_notification(
        session, tmp_path, mime_type="application/pdf",
    )
    with pytest.raises(ValueError, match="OUTBOUND_IMAGE_MIME_INVALID"):
        stage_whatsapp_artifact_notification(
            session, notification, media_store=staging,
        )


def test_manual_owner_voice_legacy_path_still_stages_without_peer_attribution(
    session, monkeypatch, tmp_path,
):
    _enable(monkeypatch, tmp_path)
    event = _event(session, event_id="wamid.manual.voice", message_type="ptt")
    payload = deepcopy(event.payload)
    payload["event_origin"] = "OWNER_MANUAL_OUTBOUND_OBSERVED"
    payload["owner_authenticated"] = False
    payload["metadata"].update({
        "from_me": True,
        "from_me_classification": "OWNER_MANUAL_OUTBOUND_OBSERVED",
        "final_from_me_classification": "OWNER_MANUAL_OUTBOUND_OBSERVED",
        "conversation_state": "READY",
        "has_media": True,
        "is_group": False,
        "owner_self_chat": False,
        "authenticated_owner_self_chat_target": False,
    })
    event.payload = payload
    session.flush()
    notification, staging = _ready_notification(
        session, tmp_path, event_id=event.external_event_id,
        mime_type="audio/ogg", media_kind="ptt",
    )
    result = stage_whatsapp_artifact_notification(
        session, notification, media_store=staging,
    )
    assert result is not None
    assert result.registration.artifact.artifact_kind == "AUDIO"
    assert result.registration.receipt.sender_actor_id is None


def test_unattributed_outbound_photo_reaches_understanding_and_original_conversation(
    session, monkeypatch, tmp_path,
):
    """Complete synthetic event->opaque Artifact->vision->conversation chain."""
    from datetime import UTC, datetime, timedelta

    from attention_router.application import services
    from attention_router.application.artifact_understanding import (
        process_artifact_understandings,
        register_artifact_understanding_notification,
    )
    from attention_router.application.conversation_live_capture import (
        reconcile_live_capture_batch,
    )
    from attention_router.infrastructure.artifact_models import ArtifactUnderstandingRow
    from attention_router.infrastructure.models import ConversationMessageRow, InboundEventRow
    from tests.test_artifact_understanding import FakeProvider
    from tests.test_conversation_live_capture import outgoing
    from tests.test_owner_reply_grace import _install_canary

    _enable(monkeypatch, tmp_path)
    _install_canary(session)
    session.commit()
    now = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(settings, "conversation_capture_enabled", True)
    monkeypatch.setattr(settings, "conversation_capture_start_at", now - timedelta(minutes=1))
    monkeypatch.setattr(settings, "conversation_capture_tenant_id", DEFAULT_TENANT_ID)
    monkeypatch.setattr(settings, "artifact_understanding_enabled", True)
    monkeypatch.setattr(settings, "artifact_understanding_provider", "openai")
    monkeypatch.setattr(settings, "artifact_understanding_model", "synthetic")
    monkeypatch.setattr(settings, "artifact_understanding_max_bytes", 1024)
    monkeypatch.setattr(settings, "persistent_memory_enabled", False)
    monkeypatch.setattr(settings, "memory_ingestion_enabled", False)

    incoming = outgoing(
        "synthetic-media-understanding-from-me", "",
        at=now, origin="UNKNOWN_FROM_ME",
    )
    incoming = incoming.model_copy(update={
        "metadata": {
            **incoming.metadata,
            "message_type": "image",
            "has_media": True,
            "conversation_state": "READY",
            "peer_id_kind": "lid",
            "outbound_provenance": None,
            "from_me_classification": "UNKNOWN_FROM_ME",
            "final_from_me_classification": "UNKNOWN_FROM_ME",
            "from_me_classification_reason": "UNMATCHED_MEDIA_FROM_ME",
            "account_outbound_media_capture_basis": ACCOUNT_BASIS,
            "authenticated_owner_self_chat_target": False,
            "owner_self_chat": False,
            "is_group": False,
        }
    })
    services.receive_normalized_inbound_event(session, incoming)
    session.commit()

    event = session.scalar(select(InboundEventRow).where(
        InboundEventRow.external_event_id == incoming.external_event_id
    ))
    assert event is not None
    conversation = session.scalar(select(ConversationMessageRow).where(
        ConversationMessageRow.source_message_id == event.external_event_id
    ))
    assert conversation is not None
    original_id = conversation.id
    thread_id = conversation.conversation_id
    assert conversation.from_me is True
    assert conversation.sender_actor_id is None
    assert conversation.external_sender_key is None
    assert conversation.text is None
    assert conversation.metadata_json["attachment_state"] == "PENDING"

    notification, staging = _ready_notification(
        session, tmp_path, event_id=event.external_event_id
    )
    result = stage_whatsapp_artifact_notification(
        session, notification, media_store=staging,
    )
    assert result is not None
    canonical_artifact = result.registration.artifact
    assert result.registration.receipt.sender_actor_id is None
    semantic_row = register_artifact_understanding_notification(
        session, notification, artifact_id=canonical_artifact.id,
    )
    assert semantic_row is not None
    assert semantic_row.status == "PENDING"
    session.commit()

    model = FakeProvider()
    process_artifact_understandings(session, "synthetic-visual-worker", provider=model)
    session.commit()
    assert model.calls == 1
    assert session.get(ArtifactUnderstandingRow, semantic_row.id).status == "READY"
    linked = reconcile_live_capture_batch(session)
    session.commit()
    assert linked.linked_artifacts == 1
    session.refresh(conversation)
    assert conversation.id == original_id
    assert conversation.conversation_id == thread_id
    assert conversation.metadata_json["artifact_id"] == canonical_artifact.id
    assert conversation.metadata_json["artifact_understanding_id"] == semantic_row.id
    assert conversation.metadata_json["artifact_understanding_status"] == "READY"
    assert conversation.metadata_json["attachment_state"] == "READY"
    assert conversation.text is None
    assert conversation.sender_actor_id is None
    assert "R$ 42" not in str(conversation.metadata_json)
    assert "DADO PRIVADO" not in str(conversation.metadata_json)
    assert reconcile_live_capture_batch(session).linked_artifacts == 0
    assert session.scalar(select(func.count()).select_from(ConversationMessageRow)) == 1
    assert session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 0
    assert session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 0
    assert session.scalar(select(func.count()).select_from(MemoryClaimRow)) == 0

def test_outbound_document_pdf_uses_existing_canonical_artifact(
    session, monkeypatch, tmp_path,
):
    _enable(monkeypatch, tmp_path)
    event = _outbound(session, kind="document")
    notification, staging = _ready_notification(
        session, tmp_path, event_id=event.external_event_id,
        mime_type="application/pdf", media_kind="document",
    )
    result = stage_whatsapp_artifact_notification(
        session, notification, media_store=staging,
    )
    assert result is not None
    assert result.registration.artifact.artifact_kind == "DOCUMENT"
    assert result.registration.receipt.sender_actor_id is None
    assert result.registration.receipt.external_receipt_id == (
        f"whatsapp:{event.external_event_id}:media"
    )
