from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from attention_router.application.pending_intent import (
    PendingIntentCandidateError,
    PendingIntentConflict,
    PendingIntentExpired,
    PendingIntentScopeError,
    attach_clarification_outbox,
    build_candidate_set,
    candidate_set_fingerprint,
    create_pending_intent,
    expire_due_pending_intents,
    find_active_pending_intent,
    mark_clarification_delivered,
    resolve_pending_intent,
    supersede_pending_intent,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import (
    AuditEventRow,
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
    PendingIntentRow,
    TenantRow,
)


STAMP = datetime(2026, 9, 19, 19, 0, tzinfo=UTC)
OWNER = "owner-fixture"
CHANNEL = "wwebjs-owner-control"
CONVERSATION = "a" * 64


def candidates():
    return [
        {
            "candidate_key": "configure-grace",
            "semantic_intent_key": "CONFIGURE_OWNER_REPLY_GRACE",
            "parameters": {"seconds": 30},
            "confidence": "high",
            "evidence_summary": "Persistent owner reply-grace interpretation.",
            "capability_mapping": {
                "status": "AVAILABLE",
                "capability_key": "owner_control.set_reply_grace",
            },
        },
        {
            "candidate_key": "one-shot-delay",
            "semantic_intent_key": "ONE_SHOT_DELAYED_REPLY",
            "parameters": {"seconds": 30},
            "confidence": "high",
            "evidence_summary": "Delay only the current reply.",
            "capability_mapping": {
                "status": "UNAVAILABLE",
                "capability_key": None,
            },
        },
    ]


def ensure_tenant(session, tenant_id):
    if session.get(TenantRow, tenant_id) is None:
        session.add(
            TenantRow(
                id=tenant_id,
                slug=tenant_id,
                name=tenant_id,
                status="ACTIVE",
                created_at=STAMP,
                updated_at=STAMP,
            )
        )
        session.flush()


def source(session, suffix, *, tenant_id=DEFAULT_TENANT_ID, received_at=STAMP):
    ensure_tenant(session, tenant_id)
    interaction = InteractionRow(
        id=f"interaction-{suffix}",
        tenant_id=tenant_id,
        event_type="OWNER_CONTROL_COMMAND",
        contact_id=OWNER,
        contact_name="Owner",
        relationship_category="owner",
        active_context=None,
        inbound_text=f"fixture {suffix}",
        state="COMPLETED",
        policy_id=None,
        policy_version_id=None,
        correlation_id=f"correlation-{suffix}",
        causation_id=None,
        lia_speech=None,
        created_at=received_at,
        updated_at=received_at,
    )
    event = InboundEventRow(
        id=f"event-{suffix}",
        tenant_id=tenant_id,
        source="wwebjs",
        external_event_id=f"external-{suffix}",
        event_type="message",
        payload={"fixture": suffix},
        payload_hash=f"hash-{suffix}",
        received_at=received_at,
        processed_at=received_at,
        interaction_id=interaction.id,
        status="PROCESSED",
        error=None,
        correlation_id=f"correlation-{suffix}",
        lineage_classification="ORGANIC",
    )
    session.add_all([interaction, event])
    session.flush()
    return event, interaction


def create_fixture(
    session,
    suffix,
    *,
    conversation=CONVERSATION,
    timestamp=STAMP,
    expires_at=None,
    candidate_values=None,
):
    event, interaction = source(session, suffix, received_at=timestamp)
    return create_pending_intent(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        represented_owner_actor_key=OWNER,
        source_inbound_event_id=event.id,
        source_interaction_id=interaction.id,
        source_channel=CHANNEL,
        conversation_key_hash=conversation,
        semantic_registry_version="owner-control-semantic-v1",
        ambiguity_reason="MATERIAL_ALTERNATIVES",
        candidates=candidate_values or candidates(),
        expires_at=expires_at or (timestamp + timedelta(minutes=5)),
        provenance={"fixture": True},
        timestamp=timestamp,
    )


def test_candidate_set_fingerprint_is_deterministic_and_keeps_unavailable_intent():
    first = build_candidate_set(
        candidates(),
        semantic_registry_version="owner-control-semantic-v1",
    )
    second = build_candidate_set(
        candidates(),
        semantic_registry_version="owner-control-semantic-v1",
    )
    assert candidate_set_fingerprint(first) == candidate_set_fingerprint(second)
    assert first["candidates"][1]["capability_mapping"]["status"] == "UNAVAILABLE"
    assert first["candidates"][1]["semantic_intent_key"] == "ONE_SHOT_DELAYED_REPLY"


def test_candidate_contract_rejects_duplicates_and_invented_shape():
    duplicate = candidates()
    duplicate[1]["candidate_key"] = duplicate[0]["candidate_key"]
    with pytest.raises(PendingIntentCandidateError, match="CANDIDATE_KEY_DUPLICATE"):
        build_candidate_set(duplicate, semantic_registry_version="v1")

    invented = candidates()
    invented[0]["arbitrary_tool"] = "RUN_SHELL"
    with pytest.raises(PendingIntentCandidateError, match="EXTRA_FIELDS"):
        build_candidate_set(invented, semantic_registry_version="v1")


def test_create_is_idempotent_for_same_source_and_candidate_set(session):
    row = create_fixture(session, "create")
    replay = create_pending_intent(
        session,
        tenant_id=row.tenant_id,
        represented_owner_actor_key=row.represented_owner_actor_key,
        source_inbound_event_id=row.source_inbound_event_id,
        source_interaction_id=row.source_interaction_id,
        source_channel=row.source_channel,
        conversation_key_hash=row.conversation_key_hash,
        semantic_registry_version=row.semantic_registry_version,
        ambiguity_reason=row.ambiguity_reason,
        candidates=candidates(),
        expires_at=row.expires_at,
        provenance={"ignored_on_exact_replay": True},
        timestamp=STAMP + timedelta(seconds=1),
    )
    assert replay.id == row.id
    assert replay.version == 1
    assert session.scalar(select(func.count()).select_from(PendingIntentRow)) == 1
    assert (
        session.scalar(
            select(func.count())
            .select_from(AuditEventRow)
            .where(AuditEventRow.event_type == "intent_clarification.created")
        )
        == 1
    )


def test_same_source_with_changed_candidate_set_fails_closed(session):
    row = create_fixture(session, "source-conflict")
    changed = candidates()
    changed[0]["parameters"] = {"seconds": 45}
    with pytest.raises(PendingIntentConflict, match="SOURCE_REPLAY_CONFLICT"):
        create_pending_intent(
            session,
            tenant_id=row.tenant_id,
            represented_owner_actor_key=row.represented_owner_actor_key,
            source_inbound_event_id=row.source_inbound_event_id,
            source_interaction_id=row.source_interaction_id,
            source_channel=row.source_channel,
            conversation_key_hash=row.conversation_key_hash,
            semantic_registry_version=row.semantic_registry_version,
            ambiguity_reason=row.ambiguity_reason,
            candidates=changed,
            expires_at=row.expires_at,
            timestamp=STAMP + timedelta(seconds=1),
        )


def test_one_active_intent_per_scope_requires_explicit_supersession(session):
    first = create_fixture(session, "first")
    second_event, second_interaction = source(
        session,
        "second",
        received_at=STAMP + timedelta(seconds=1),
    )
    with pytest.raises(PendingIntentConflict, match="ACTIVE_SCOPE_CONFLICT"):
        create_pending_intent(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            represented_owner_actor_key=OWNER,
            source_inbound_event_id=second_event.id,
            source_interaction_id=second_interaction.id,
            source_channel=CHANNEL,
            conversation_key_hash=CONVERSATION,
            semantic_registry_version="owner-control-semantic-v1",
            ambiguity_reason="MATERIAL_ALTERNATIVES",
            candidates=candidates(),
            expires_at=STAMP + timedelta(minutes=5),
            timestamp=STAMP + timedelta(seconds=1),
        )

    supersede_pending_intent(
        session,
        pending_intent_id=first.id,
        superseding_source_inbound_event_id=second_event.id,
        timestamp=STAMP + timedelta(seconds=2),
    )
    second = create_pending_intent(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        represented_owner_actor_key=OWNER,
        source_inbound_event_id=second_event.id,
        source_interaction_id=second_interaction.id,
        source_channel=CHANNEL,
        conversation_key_hash=CONVERSATION,
        semantic_registry_version="owner-control-semantic-v1",
        ambiguity_reason="MATERIAL_ALTERNATIVES",
        candidates=candidates(),
        expires_at=STAMP + timedelta(minutes=5),
        timestamp=STAMP + timedelta(seconds=2),
    )
    assert first.state == "SUPERSEDED"
    assert second.state == "PENDING"


def test_expiry_is_terminal_and_active_lookup_excludes_it(session):
    row = create_fixture(
        session,
        "expire",
        expires_at=STAMP + timedelta(seconds=10),
    )
    assert (
        find_active_pending_intent(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            represented_owner_actor_key=OWNER,
            source_channel=CHANNEL,
            conversation_key_hash=CONVERSATION,
            timestamp=STAMP + timedelta(seconds=5),
        ).id
        == row.id
    )
    assert expire_due_pending_intents(
        session,
        timestamp=STAMP + timedelta(seconds=11),
    ) == 1
    assert row.state == "EXPIRED"
    assert row.version == 2
    assert (
        find_active_pending_intent(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            represented_owner_actor_key=OWNER,
            source_channel=CHANNEL,
            conversation_key_hash=CONVERSATION,
            timestamp=STAMP + timedelta(seconds=11),
        )
        is None
    )


def test_resolution_selects_frozen_candidate_and_is_idempotent(session):
    row = create_fixture(session, "resolve")
    resolution, _ = source(
        session,
        "resolution",
        received_at=STAMP + timedelta(seconds=2),
    )
    resolved = resolve_pending_intent(
        session,
        pending_intent_id=row.id,
        tenant_id=DEFAULT_TENANT_ID,
        represented_owner_actor_key=OWNER,
        source_channel=CHANNEL,
        conversation_key_hash=CONVERSATION,
        resolution_inbound_event_id=resolution.id,
        selected_candidate_key="one-shot-delay",
        resolution_kind="EXPLICIT_SELECTION",
        timestamp=STAMP + timedelta(seconds=3),
    )
    version = resolved.version
    duplicate = resolve_pending_intent(
        session,
        pending_intent_id=row.id,
        tenant_id=DEFAULT_TENANT_ID,
        represented_owner_actor_key=OWNER,
        source_channel=CHANNEL,
        conversation_key_hash=CONVERSATION,
        resolution_inbound_event_id=resolution.id,
        selected_candidate_key="one-shot-delay",
        resolution_kind="EXPLICIT_SELECTION",
        timestamp=STAMP + timedelta(seconds=4),
    )
    assert duplicate.id == row.id
    assert duplicate.version == version
    assert row.state == "RESOLVED"
    assert row.selected_candidate_key == "one-shot-delay"
    assert row.resolution_inbound_event_id == resolution.id

    with pytest.raises(PendingIntentConflict, match="ALREADY_RESOLVED"):
        resolve_pending_intent(
            session,
            pending_intent_id=row.id,
            tenant_id=DEFAULT_TENANT_ID,
            represented_owner_actor_key=OWNER,
            source_channel=CHANNEL,
            conversation_key_hash=CONVERSATION,
            resolution_inbound_event_id=resolution.id,
            selected_candidate_key="configure-grace",
            resolution_kind="EXPLICIT_SELECTION",
            timestamp=STAMP + timedelta(seconds=5),
        )


def test_resolution_scope_mismatch_fails_without_mutation(session):
    row = create_fixture(session, "scope")
    resolution, _ = source(
        session,
        "scope-resolution",
        received_at=STAMP + timedelta(seconds=2),
    )
    with pytest.raises(PendingIntentScopeError, match="RESOLUTION_SCOPE_MISMATCH"):
        resolve_pending_intent(
            session,
            pending_intent_id=row.id,
            tenant_id=DEFAULT_TENANT_ID,
            represented_owner_actor_key="another-owner",
            source_channel=CHANNEL,
            conversation_key_hash=CONVERSATION,
            resolution_inbound_event_id=resolution.id,
            selected_candidate_key="configure-grace",
            resolution_kind="EXPLICIT_SELECTION",
            timestamp=STAMP + timedelta(seconds=3),
        )
    assert row.state == "PENDING"
    assert row.selected_candidate_key is None


def test_resolution_event_can_resolve_only_one_pending_intent(session):
    first = create_fixture(session, "resolve-once-a", conversation="a" * 64)
    second = create_fixture(session, "resolve-once-b", conversation="b" * 64)
    resolution, _ = source(
        session,
        "shared-resolution",
        received_at=STAMP + timedelta(seconds=2),
    )
    resolve_pending_intent(
        session,
        pending_intent_id=first.id,
        tenant_id=DEFAULT_TENANT_ID,
        represented_owner_actor_key=OWNER,
        source_channel=CHANNEL,
        conversation_key_hash="a" * 64,
        resolution_inbound_event_id=resolution.id,
        selected_candidate_key="configure-grace",
        resolution_kind="EXPLICIT_SELECTION",
        timestamp=STAMP + timedelta(seconds=3),
    )
    with pytest.raises(PendingIntentConflict, match="RESOLUTION_EVENT_ALREADY_USED"):
        resolve_pending_intent(
            session,
            pending_intent_id=second.id,
            tenant_id=DEFAULT_TENANT_ID,
            represented_owner_actor_key=OWNER,
            source_channel=CHANNEL,
            conversation_key_hash="b" * 64,
            resolution_inbound_event_id=resolution.id,
            selected_candidate_key="configure-grace",
            resolution_kind="EXPLICIT_SELECTION",
            timestamp=STAMP + timedelta(seconds=3),
        )
    assert second.state == "PENDING"


def test_resolution_after_expiry_terminalizes_without_selection(session):
    row = create_fixture(
        session,
        "resolve-expired",
        expires_at=STAMP + timedelta(seconds=1),
    )
    resolution, _ = source(
        session,
        "resolve-expired-answer",
        received_at=STAMP + timedelta(seconds=2),
    )
    with pytest.raises(PendingIntentExpired, match="PENDING_INTENT_EXPIRED"):
        resolve_pending_intent(
            session,
            pending_intent_id=row.id,
            tenant_id=DEFAULT_TENANT_ID,
            represented_owner_actor_key=OWNER,
            source_channel=CHANNEL,
            conversation_key_hash=CONVERSATION,
            resolution_inbound_event_id=resolution.id,
            selected_candidate_key="configure-grace",
            resolution_kind="EXPLICIT_SELECTION",
            timestamp=STAMP + timedelta(seconds=2),
        )
    assert row.state == "EXPIRED"
    assert row.selected_candidate_key is None


def test_clarification_outbox_link_and_delivery_are_idempotent(session):
    row = create_fixture(session, "outbox")
    outbox = OutboxMessageRow(
        id="clarification-outbox",
        interaction_id=row.source_interaction_id,
        action_type="intent_clarification_text",
        destination="local_transport",
        payload={"text": "Você quer A ou B?"},
        status="PENDING",
        created_at=STAMP,
        available_at=STAMP,
        claimed_at=None,
        claimed_by=None,
        attempt_count=0,
        last_error=None,
        completed_at=None,
        idempotency_key="intent-clarification:fixture",
        correlation_id=row.correlation_id,
        causation_id=row.source_inbound_event_id,
        execution_intent_id=None,
    )
    session.add(outbox)
    session.flush()

    attach_clarification_outbox(
        session,
        pending_intent_id=row.id,
        outbox_id=outbox.id,
        timestamp=STAMP + timedelta(seconds=1),
    )
    version = row.version
    attach_clarification_outbox(
        session,
        pending_intent_id=row.id,
        outbox_id=outbox.id,
        timestamp=STAMP + timedelta(seconds=2),
    )
    assert row.version == version

    mark_clarification_delivered(
        session,
        pending_intent_id=row.id,
        outbox_id=outbox.id,
        timestamp=STAMP + timedelta(seconds=3),
    )
    delivered_version = row.version
    mark_clarification_delivered(
        session,
        pending_intent_id=row.id,
        outbox_id=outbox.id,
        timestamp=STAMP + timedelta(seconds=4),
    )
    assert row.version == delivered_version
    assert row.clarification_delivered_at == STAMP + timedelta(seconds=3)
