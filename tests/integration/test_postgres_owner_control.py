from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading
import uuid

import pytest
from sqlalchemy import func, select

from attention_router.adapters.inbound import NormalizedInboundEvent
from attention_router.application import services
from attention_router.domain.models import now_utc
from attention_router.infrastructure.models import (
    ActorBindingRow,
    InteractionRow,
    OutboxMessageRow,
    OwnerOperationalControlChangeRow,
    OwnerOperationalControlRow,
    PolicyRow,
    TenantRow,
)
from attention_router.infrastructure.repository import ensure_policy_version


pytestmark = pytest.mark.postgres


def _install(Session, suffix: str) -> dict[str, str]:
    tenant_id = str(uuid.uuid4())
    owner_actor_id = f"owner-control-actor-{suffix}"
    owner_external_id = f"owner-control-{suffix}@c.us"
    policy_id = f"owner_control_grace_{suffix}"
    stamp = now_utc()
    config = {
        "identifier": policy_id,
        "name": f"Owner Control Grace {suffix}",
        "match_criteria": {"audience": f"owner_control_{suffix}"},
        "priority": 100,
        "specificity": 100,
        "tone": "cordial",
        "initial_wait_seconds": 1,
        "allowed_disclosures": [],
        "allowed_actions": ["respond"],
        "escalation_steps": ["respond"],
        "ack_timeout_seconds": 30,
        "repetition_limit": 1,
        "cancellation_conditions": ["human_reply"],
        "completion_conditions": ["safe_completion"],
        "owner_reply_grace_allowed": True,
        "owner_reply_grace_default_seconds": 30,
        "owner_reply_grace_min_seconds": 0,
        "owner_reply_grace_max_seconds": 300,
        "owner_reply_grace_mode": "TRAILING_EDGE",
    }
    with Session() as session:
        session.add(
            TenantRow(
                id=tenant_id,
                slug=f"owner_control_{suffix}",
                name=f"Owner Control {suffix}",
                status="ACTIVE",
                created_at=stamp,
                updated_at=stamp,
            )
        )
        session.flush()
        session.add(
            ActorBindingRow(
                id=f"owner-control-binding-{suffix}",
                tenant_id=tenant_id,
                source="wwebjs",
                external_actor_id=owner_external_id,
                actor_key=owner_actor_id,
                display_name="Owner",
                actor_category="owner",
                active_context=None,
                is_active=True,
                binding_metadata={"owner": True},
                created_at=stamp,
                updated_at=stamp,
            )
        )
        policy = PolicyRow(
            **{
                key: config[key]
                for key in PolicyRow.__table__.columns.keys()
                if key in config
            },
            tenant_id=tenant_id,
            is_active=True,
        )
        session.add(policy)
        session.flush()
        ensure_policy_version(session, policy, config, "owner-control-postgres-test")
        session.commit()
    return {
        "tenant_id": tenant_id,
        "owner_actor_id": owner_actor_id,
        "owner_external_id": owner_external_id,
        "policy_id": policy_id,
    }


def _event(context: dict[str, str], event_id: str, text: str) -> NormalizedInboundEvent:
    stamp = datetime(2026, 9, 4, 18, 0, tzinfo=timezone.utc)
    return NormalizedInboundEvent(
        tenant_id=context["tenant_id"],
        source="wwebjs",
        external_event_id=event_id,
        event_type="message",
        occurred_at=stamp,
        received_at=stamp,
        actor_id=context["owner_external_id"],
        actor_display_name="Owner",
        actor_category="owner",
        channel="whatsapp",
        content=text,
        event_origin="OWNER_COMMAND",
        owner_authenticated=True,
        metadata={
            "from_me": True,
            "owner_self_chat": True,
            "from_me_classification": "OWNER_COMMAND",
            "final_from_me_classification": "OWNER_COMMAND",
        },
    )


def test_same_owner_control_source_event_is_single_mutation_and_outbox(Session):
    suffix = uuid.uuid4().hex[:10]
    context = _install(Session, suffix)
    barrier = threading.Barrier(2)

    def receive():
        with Session() as session:
            barrier.wait()
            result = services.receive_normalized_inbound_event(
                session,
                _event(context, f"same-{suffix}", "espera 60"),
            )
            session.commit()
            return result["id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        interaction_ids = list(pool.map(lambda _index: receive(), range(2)))

    assert len(set(interaction_ids)) == 1
    with Session() as session:
        interaction_subquery = select(InteractionRow.id).where(
            InteractionRow.tenant_id == context["tenant_id"]
        )
        assert session.scalar(
            select(func.count()).select_from(OwnerOperationalControlChangeRow).where(
                OwnerOperationalControlChangeRow.tenant_id == context["tenant_id"]
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(InteractionRow).where(
                InteractionRow.tenant_id == context["tenant_id"]
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(OutboxMessageRow).where(
                OutboxMessageRow.interaction_id.in_(interaction_subquery)
            )
        ) == 1
        assert session.scalar(
            select(OwnerOperationalControlRow.revision).where(
                OwnerOperationalControlRow.tenant_id == context["tenant_id"]
            )
        ) == 1


def test_distinct_owner_control_commands_linearize_revisions(Session):
    suffix = uuid.uuid4().hex[:10]
    context = _install(Session, suffix)
    barrier = threading.Barrier(2)

    def receive(index: int):
        with Session() as session:
            barrier.wait()
            services.receive_normalized_inbound_event(
                session,
                _event(
                    context,
                    f"distinct-{suffix}-{index}",
                    f"espera {60 + index * 30}",
                ),
            )
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(receive, range(2)))

    with Session() as session:
        control = session.scalar(
            select(OwnerOperationalControlRow).where(
                OwnerOperationalControlRow.tenant_id == context["tenant_id"]
            )
        )
        changes = session.scalars(
            select(OwnerOperationalControlChangeRow)
            .where(OwnerOperationalControlChangeRow.tenant_id == context["tenant_id"])
            .order_by(OwnerOperationalControlChangeRow.resulting_revision)
        ).all()
        interaction_subquery = select(InteractionRow.id).where(
            InteractionRow.tenant_id == context["tenant_id"]
        )
        assert control.revision == 2
        assert control.integer_value in {60, 90}
        assert [change.resulting_revision for change in changes] == [1, 2]
        assert session.scalar(
            select(func.count()).select_from(OutboxMessageRow).where(
                OutboxMessageRow.interaction_id.in_(interaction_subquery)
            )
        ) == 2
