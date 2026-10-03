from datetime import UTC, datetime

from sqlalchemy import select

from attention_router.adapters.inbound import NormalizedInboundEvent
from attention_router.application import services
from attention_router.application.platform.context import (
    resolve_represented_subject_identity,
)
from attention_router.application.sensitive_disclosure import (
    DISCLOSURE_OUTBOX_ACTION,
    prepare_location_disclosure,
    process_sensitive_disclosures,
)
from attention_router.config import settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.client_bootstrap_models import (
    ClientDeviceRow,
    ClientTenantMembershipRow,
)
from attention_router.infrastructure.client_location_models import (
    ClientLocationSnapshotRow,
)
from attention_router.infrastructure.human_identity_models import (
    HumanIdentityRow,
    HumanProfileRow,
)
from attention_router.infrastructure.models import (
    HumanExecutionAuthorizationRow,
    InteractionRow,
    OutboxMessageRow,
    SensitiveDisclosureRequestRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding


def _seed_conversation(session):
    now = datetime.now(UTC)
    human_id = "hid_disclosure_owner"
    session.add(HumanIdentityRow(id=human_id, created_at=now))
    session.add(
        ClientTenantMembershipRow(
            id="ctm_disclosure_owner",
            human_identity_id=human_id,
            tenant_id=DEFAULT_TENANT_ID,
            role="OWNER",
            status="ACTIVE",
            created_at=now,
            updated_at=now,
        )
    )
    session.add(
        HumanProfileRow(
            human_identity_id=human_id,
            assistant_reference_name="Leonardo",
            created_at=now,
            updated_at=now,
        )
    )
    upsert_actor_binding(
        session,
        source="test",
        external_actor_id="owner-external",
        actor_key="owner-leonardo",
        actor_category="owner",
        display_name="Leonardo",
        metadata={"owner": True},
    )
    binding = upsert_actor_binding(
        session,
        source="test",
        external_actor_id="father-external",
        actor_key="father",
        actor_category="family_core",
        display_name="Sr. Francisco",
        metadata={"relationship": "pai"},
    )
    session.flush()
    event = NormalizedInboundEvent(
        source="test",
        external_event_id="location-request-event",
        event_type="message",
        actor_id="father-external",
        actor_display_name="Sr. Francisco",
        actor_category="family_core",
        channel="whatsapp",
        content="qual sua localização?",
    )
    received = services.receive_normalized_inbound_event(session, event)
    session.flush()
    interaction = session.get(InteractionRow, received["id"])
    identity = resolve_represented_subject_identity(
        session,
        DEFAULT_TENANT_ID,
    )
    assert identity is not None
    return now, human_id, binding, interaction, received, identity


def test_location_request_creates_android_approval_without_reading_location(
    session,
    monkeypatch,
):
    now, _human_id, binding, interaction, received, identity = _seed_conversation(
        session
    )
    monkeypatch.setattr(settings, "client_approval_enabled", True)
    monkeypatch.setattr(settings, "client_session_enabled", True)

    prepared = prepare_location_disclosure(
        session,
        interaction=interaction,
        source_event_id=received["inbound_event_id"],
        binding=binding,
        represented_identity=identity,
        recipient_reference="father@lid",
        parameters={},
        now=now,
    )

    assert prepared.status == "PENDING_APPROVAL"
    assert "Leonardo" in prepared.response_text
    request = session.scalar(select(SensitiveDisclosureRequestRow))
    assert request is not None
    assert request.requester_display_name == "Sr. Francisco"
    assert request.requester_relationship == "pai"
    assert request.represented_owner_reference_name == "Leonardo"
    authorization = session.get(
        HumanExecutionAuthorizationRow,
        request.authorization_id,
    )
    assert authorization.state == "PENDING_HUMAN_APPROVAL"
    assert authorization.approval_channel == "android_client"
    assert session.scalars(select(OutboxMessageRow)).all() == []


def test_approved_location_is_read_once_and_enqueued_back_to_requester(
    session,
    monkeypatch,
):
    now, human_id, binding, interaction, received, identity = _seed_conversation(
        session
    )
    monkeypatch.setattr(settings, "client_approval_enabled", True)
    monkeypatch.setattr(settings, "client_session_enabled", True)
    prepared = prepare_location_disclosure(
        session,
        interaction=interaction,
        source_event_id=received["inbound_event_id"],
        binding=binding,
        represented_identity=identity,
        recipient_reference="father@lid",
        parameters={},
        now=now,
    )
    request = session.get(SensitiveDisclosureRequestRow, prepared.request_id)
    authorization = session.get(
        HumanExecutionAuthorizationRow,
        request.authorization_id,
    )
    authorization.state = "APPROVED"
    authorization.decision_at = now
    authorization.updated_at = now
    session.add(
        ClientDeviceRow(
            id="cdev_disclosure_owner",
            human_identity_id=human_id,
            public_key_fingerprint=("sha256:" + "a" * 64)[:71],
            public_key_spki=b"synthetic-spki",
            canonical_name="Synthetic Android",
            platform="ANDROID",
            roles=["CLIENT", "CAPABILITY_NODE"],
            status="ACTIVE",
            created_at=now,
            updated_at=now,
        )
    )
    session.add(
        ClientLocationSnapshotRow(
            id="cloc_disclosure_owner",
            human_identity_id=human_id,
            device_id="cdev_disclosure_owner",
            tenant_id=DEFAULT_TENANT_ID,
            latitude=-3.75,
            longitude=-38.58,
            accuracy_m=8.0,
            precision="PRECISE",
            captured_at=now,
            received_at=now,
        )
    )
    session.flush()

    assert process_sensitive_disclosures(session, now=now) == 1
    session.flush()

    outbox = session.scalar(
        select(OutboxMessageRow).where(
            OutboxMessageRow.action_type == DISCLOSURE_OUTBOX_ACTION
        )
    )
    assert outbox is not None
    assert outbox.payload["external_actor_id"] == "father@lid"
    assert outbox.payload["text"].startswith("[Andy] ")
    assert "Leonardo" in outbox.payload["text"]
    assert "-3.750000" in outbox.payload["text"]
    assert authorization.state == "APPROVED"
    assert request.state == "RESPONSE_PENDING"


def test_denied_location_never_reads_provider_and_enqueues_denial(
    session,
    monkeypatch,
):
    now, _human_id, binding, interaction, received, identity = _seed_conversation(
        session
    )
    monkeypatch.setattr(settings, "client_approval_enabled", True)
    monkeypatch.setattr(settings, "client_session_enabled", True)
    prepared = prepare_location_disclosure(
        session,
        interaction=interaction,
        source_event_id=received["inbound_event_id"],
        binding=binding,
        represented_identity=identity,
        recipient_reference="father@lid",
        parameters={},
        now=now,
    )
    request = session.get(SensitiveDisclosureRequestRow, prepared.request_id)
    authorization = session.get(
        HumanExecutionAuthorizationRow,
        request.authorization_id,
    )
    authorization.state = "DENIED"
    authorization.decision_at = now
    authorization.updated_at = now

    assert process_sensitive_disclosures(session, now=now) == 1
    outbox = session.scalar(
        select(OutboxMessageRow).where(
            OutboxMessageRow.action_type == DISCLOSURE_OUTBOX_ACTION
        )
    )
    assert outbox is not None
    assert "não foi autorizada" in outbox.payload["text"]
    assert request.result == {"decision": "DENIED"}
