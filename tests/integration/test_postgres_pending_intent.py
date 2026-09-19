from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import threading
import uuid

import pytest
from sqlalchemy import func, select

from attention_router.application.pending_intent import (
    PendingIntentConflict,
    create_pending_intent,
    resolve_pending_intent,
)
from attention_router.domain.models import new_id
from attention_router.infrastructure.models import (
    InboundEventRow,
    InteractionRow,
    PendingIntentRow,
    TenantRow,
)


pytestmark = pytest.mark.postgres

STAMP = datetime(2026, 9, 19, 19, 30, tzinfo=UTC)
CHANNEL = "wwebjs-owner-control"


def _candidates():
    return [
        {
            "candidate_key": "configure-grace",
            "semantic_intent_key": "CONFIGURE_OWNER_REPLY_GRACE",
            "parameters": {"seconds": 30},
            "confidence": "high",
            "evidence_summary": "Persistent configuration.",
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
            "evidence_summary": "One-shot reply delay.",
            "capability_mapping": {
                "status": "UNAVAILABLE",
                "capability_key": None,
            },
        },
    ]


def _install(Session):
    tenant_id = str(uuid.uuid4())
    owner = f"owner-{uuid.uuid4().hex[:10]}"
    with Session() as session:
        session.add(
            TenantRow(
                id=tenant_id,
                slug=f"pending-intent-{uuid.uuid4().hex[:10]}",
                name="Pending Intent Test",
                status="ACTIVE",
                created_at=STAMP,
                updated_at=STAMP,
            )
        )
        sources = []
        for suffix, offset in [("a", 0), ("b", 1), ("resolution", 2)]:
            stamp = STAMP + timedelta(seconds=offset)
            interaction = InteractionRow(
                id=new_id(),
                tenant_id=tenant_id,
                event_type="OWNER_CONTROL_COMMAND",
                contact_id=owner,
                contact_name="Owner",
                relationship_category="owner",
                inbound_text=f"fixture-{suffix}",
                state="COMPLETED",
                correlation_id=new_id(),
                created_at=stamp,
                updated_at=stamp,
            )
            session.add(interaction)
            session.flush()
            event = InboundEventRow(
                id=new_id(),
                tenant_id=tenant_id,
                source="wwebjs",
                external_event_id=f"pending-intent-{suffix}-{uuid.uuid4().hex}",
                event_type="message",
                payload={"fixture": suffix},
                payload_hash=uuid.uuid4().hex,
                received_at=stamp,
                processed_at=stamp,
                interaction_id=interaction.id,
                status="PROCESSED",
                correlation_id=interaction.correlation_id,
                lineage_classification="ORGANIC",
            )
            session.add(event)
            session.flush()
            sources.append((event.id, interaction.id))
        session.commit()
    return tenant_id, owner, sources


def _create(
    session,
    *,
    tenant_id,
    owner,
    event_id,
    interaction_id,
    conversation,
    timestamp,
):
    return create_pending_intent(
        session,
        tenant_id=tenant_id,
        represented_owner_actor_key=owner,
        source_inbound_event_id=event_id,
        source_interaction_id=interaction_id,
        source_channel=CHANNEL,
        conversation_key_hash=conversation,
        semantic_registry_version="owner-control-semantic-v1",
        ambiguity_reason="MATERIAL_ALTERNATIVES",
        candidates=_candidates(),
        expires_at=timestamp + timedelta(minutes=5),
        timestamp=timestamp,
    )


def test_postgres_partial_unique_allows_only_one_active_scope(Session):
    tenant_id, owner, sources = _install(Session)
    barrier = threading.Barrier(2)

    def attempt(index):
        event_id, interaction_id = sources[index]
        with Session() as session:
            barrier.wait(timeout=10)
            try:
                _create(
                    session,
                    tenant_id=tenant_id,
                    owner=owner,
                    event_id=event_id,
                    interaction_id=interaction_id,
                    conversation="a" * 64,
                    timestamp=STAMP + timedelta(seconds=index),
                )
                session.commit()
                return "created"
            except PendingIntentConflict:
                session.rollback()
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))

    assert sorted(results) == ["conflict", "created"]
    with Session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(PendingIntentRow)
                .where(
                    PendingIntentRow.tenant_id == tenant_id,
                    PendingIntentRow.state == "PENDING",
                )
            )
            == 1
        )


def test_postgres_resolution_event_is_single_use_under_concurrency(Session):
    tenant_id, owner, sources = _install(Session)
    with Session() as session:
        first = _create(
            session,
            tenant_id=tenant_id,
            owner=owner,
            event_id=sources[0][0],
            interaction_id=sources[0][1],
            conversation="a" * 64,
            timestamp=STAMP,
        )
        second = _create(
            session,
            tenant_id=tenant_id,
            owner=owner,
            event_id=sources[1][0],
            interaction_id=sources[1][1],
            conversation="b" * 64,
            timestamp=STAMP + timedelta(seconds=1),
        )
        session.commit()
        intent_ids = [(first.id, "a" * 64), (second.id, "b" * 64)]

    resolution_event_id = sources[2][0]
    barrier = threading.Barrier(2)

    def resolve(item):
        intent_id, conversation = item
        with Session() as session:
            barrier.wait(timeout=10)
            try:
                resolve_pending_intent(
                    session,
                    pending_intent_id=intent_id,
                    tenant_id=tenant_id,
                    represented_owner_actor_key=owner,
                    source_channel=CHANNEL,
                    conversation_key_hash=conversation,
                    resolution_inbound_event_id=resolution_event_id,
                    selected_candidate_key="configure-grace",
                    resolution_kind="EXPLICIT_SELECTION",
                    timestamp=STAMP + timedelta(seconds=3),
                )
                session.commit()
                return "resolved"
            except PendingIntentConflict:
                session.rollback()
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(resolve, intent_ids))

    assert sorted(results) == ["conflict", "resolved"]
    with Session() as session:
        rows = session.scalars(
            select(PendingIntentRow).where(PendingIntentRow.tenant_id == tenant_id)
        ).all()
        assert sum(row.state == "RESOLVED" for row in rows) == 1
        assert sum(row.state == "PENDING" for row in rows) == 1
        assert sum(
            row.resolution_inbound_event_id == resolution_event_id for row in rows
        ) == 1
