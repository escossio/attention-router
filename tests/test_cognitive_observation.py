from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from attention_router.application.cognitive_observation import observe_attachment_context
from attention_router.application.conversation_live_capture import (
    attach_canonical_artifact,
    capture_live_receipt,
)
from attention_router.application.platform.artifacts import ArtifactReceiptInput, register_artifact_receipt
from attention_router.application.platform.events import normalize_inbound_event
from attention_router.config import settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id
from attention_router.infrastructure.candidate_insight_models import CandidateInsightRow
from attention_router.infrastructure.client_bootstrap_models import ClientTenantMembershipRow
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.models import (
    ActorBindingRow,
    AgentExecutionIntentRow,
    InteractionRow,
    ConversationMessageRow,
    ExecutionIntentRow,
    FactRow,
    InboundEventRow,
    MemoryClaimRow,
    OutboxMessageRow,
    RelationshipRow,
    TenantRow,
    TimelineEventRow,
)
from attention_router.infrastructure.semantic_episode_models import SemanticEpisodeRow

START = datetime(2026, 11, 19, tzinfo=UTC)
ACCOUNT = "fixture-account"


def _attachment(
    session, name, day, *, tenant_id=DEFAULT_TENANT_ID, account=ACCOUNT, content_sha256="a" * 64,
    content=None, conversation="wwebjs:fixture", sender="fixture-peer", media_kind="image",
):
    stamp = START + timedelta(days=day)
    payload = {
        "channel": "whatsapp",
        "event_origin": "EXTERNAL_INBOUND",
        "actor_id": sender,
        "occurred_at": stamp.isoformat(),
        "metadata": {
            "from_me": False,
            "conversation_state": "READY",
            "conversation_key": conversation,
            "source_account": account,
            "message_type": media_kind,
            "has_media": True,
        },
    }
    if content is not None:
        payload["content"] = content
    # Isolated fixture that exercises the trusted ingress contract.
    inbound = InboundEventRow(
        id=new_id(), tenant_id=tenant_id, source="wwebjs", external_event_id=name,
        event_type="message", payload=payload, payload_hash=stable_hash(payload),
        received_at=stamp, status="PROCESSED", correlation_id=new_id(),
        lineage_classification="ORGANIC",
    )
    session.add(inbound)
    session.flush()
    normalize_inbound_event(session, inbound)
    capture = capture_live_receipt(session, inbound)
    assert capture.status == "ARCHIVED"
    artifact_kind, mime_type = {
        "image": ("IMAGE", "image/png"), "document": ("DOCUMENT", "application/pdf"),
        "ptt": ("AUDIO", "audio/ogg"),
    }[media_kind]
    registered = register_artifact_receipt(session, ArtifactReceiptInput(
        tenant_id=tenant_id, content_sha256=content_sha256, artifact_kind=artifact_kind,
        mime_type=mime_type, size_bytes=4, storage_provider="fixture",
        storage_reference="fixture", source_channel="channel.whatsapp",
        source_account=account, external_receipt_id=f"whatsapp:{name}:media", received_at=stamp,
    ))
    assert attach_canonical_artifact(session, inbound).status == "ARTIFACT_LINKED"
    return capture.message_id, inbound, registered


def _seed_observation_owner(session, monkeypatch):
    tenant_id = DEFAULT_TENANT_ID
    for binding_id, peer, category in (
        ("fixture-owner", "fixture-owner-external", "owner"),
        ("fixture-peer-binding", "fixture-peer", "known_contact"),
    ):
        session.add(ActorBindingRow(
            id=binding_id, tenant_id=tenant_id, source="wwebjs", external_actor_id=peer,
            actor_key=binding_id, actor_category=category, binding_metadata={}, is_active=True,
            created_at=START, updated_at=START,
        ))
    session.add(TenantRow(id="other", slug="other", name="other", status="ACTIVE", created_at=START, updated_at=START))
    session.flush()
    session.add(HumanIdentityRow(id="fixture-human-owner", created_at=START))
    session.flush()
    session.add(ClientTenantMembershipRow(
        id=new_id(), human_identity_id="fixture-human-owner", tenant_id=tenant_id,
        role="OWNER", status="ACTIVE", created_at=START, updated_at=START,
    ))
    session.flush()
    monkeypatch.setattr(settings, "cognitive_runtime_canary_tenant_id", tenant_id)
    monkeypatch.setattr(settings, "persistent_memory_canary_binding_id", "fixture-owner")
    monkeypatch.setattr(settings, "conversation_capture_enabled", True)
    monkeypatch.setattr(settings, "conversation_capture_start_at", START)
    monkeypatch.setattr(settings, "conversation_capture_tenant_id", None)



def _exercise(session, monkeypatch, *, prior_to_final_withdrawal=None):
    _seed_observation_owner(session, monkeypatch)
    tenant_id = DEFAULT_TENANT_ID
    first, _, registered = _attachment(session, "first", 1)
    second, _, _ = _attachment(session, "second", 2)
    third, _, _ = _attachment(session, "third", 3)
    foreign, _, _ = _attachment(session, "foreign", 1, tenant_id="other")
    wrong_account, _, _ = _attachment(session, "account", 1, account="another-fixture-account")
    synthetic, synthetic_receipt, _ = _attachment(session, "invalidated-lineage", 3)
    synthetic_receipt.lineage_classification = "SYNTHETIC"
    tampered, _, _ = _attachment(session, "tampered", 3)
    tampered_message = session.get(ConversationMessageRow, tampered)
    tampered_message.metadata_json = {**tampered_message.metadata_json, "artifact_receipt_id": registered.receipt.id}
    session.flush()

    def observe(*ids, binding_id="fixture-owner", owner="fixture-human-owner", start=START):
        return observe_attachment_context(
            session, tenant_id=tenant_id, binding_id=binding_id, source_account=ACCOUNT,
            owner_human_identity_id=owner, start=start, now=START + timedelta(days=3, hours=1), message_ids=ids,
        )

    assert registered.artifact_created
    with pytest.raises(ValueError, match="SCOPE_MISMATCH"):
        observe(first, binding_id="fixture-peer-binding")
    with pytest.raises(RuntimeError, match="OWNER_MEMBERSHIP_REQUIRED"):
        observe(first, owner="missing-human-owner")
    assert observe(first, start=START + timedelta(days=2)).linked == 0
    initial = observe(first, first, foreign, wrong_account, synthetic, tampered)
    assert initial.received == 5
    assert initial.rejected == {
        "OUTSIDE_SCOPE_OR_MISSING": 2,
        "SOURCE_CONTEXT_UNPROVEN": 1,
        "ARTIFACT_RECEIPT_UNPROVEN": 1,
    }
    assert initial.linked == initial.resources == initial.identified_people == 1
    assert initial.new_episodes == initial.new_links == 1
    assert initial.candidates == 0
    assert initial.pairs_with_single_observation == 1

    current = observe(first, second, third)
    assert current.candidates == current.new_candidates == current.recurrences == 1
    assert current.new_links == current.new_memberships == 2
    assert current.new_episodes == 0
    assert current.graph_edges > initial.graph_edges
    episode = session.scalar(select(SemanticEpisodeRow))
    assert episode.last_activity_at.replace(tzinfo=UTC) == START + timedelta(days=3)
    assert episode.provenance["anchor_event_id"]
    evidence = session.scalars(select(TimelineEventRow).where(TimelineEventRow.resource_id.is_not(None))).all()
    assert len(evidence) == 3
    assert all(event.event_ref["artifact_receipt_id"] and event.canonical_event_id for event in evidence)
    assert session.scalar(select(CandidateInsightRow)).state == "PROPOSED"

    replay = observe(third, second, first)
    assert replay.linked == 3
    assert replay.new_links == replay.new_candidates == replay.new_memberships == 0
    if prior_to_final_withdrawal is not None:
        prior_to_final_withdrawal(session, observe, (first, second, third), registered)
        return
    registered.artifact.status = "DELETED"
    withheld = observe(first, second, third)
    assert withheld.linked == withheld.candidates == 0
    assert withheld.rejected == {"ARTIFACT_UNAVAILABLE": 3}
    for model in (
        OutboxMessageRow, ExecutionIntentRow, AgentExecutionIntentRow, InteractionRow,
        RelationshipRow, FactRow, MemoryClaimRow,
    ):
        assert session.scalar(select(func.count()).select_from(model)) == 0


def test_attachment_context(session, monkeypatch):
    _exercise(session, monkeypatch)
