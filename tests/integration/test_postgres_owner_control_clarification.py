from __future__ import annotations

from datetime import timedelta
import uuid

import pytest
from sqlalchemy import select

from attention_router.adapters.inbound import NormalizedInboundEvent
from attention_router.adapters.wwebjs_owner_control import (
    OwnerControlParseResult,
    OwnerControlParseStatus,
)
from attention_router.application import services
from attention_router.application.owner_control_semantic_registry import (
    OWNER_CONTROL_SEMANTIC_REGISTRY_VERSION,
    build_registered_semantic_candidate,
)
from attention_router.application.pending_intent import build_candidate_set
from attention_router.config import settings
from attention_router.domain.models import now_utc
from attention_router.infrastructure.models import (
    OwnerOperationalControlRow,
    PendingIntentRow,
)
from tests.integration.test_postgres_owner_control import _install


pytestmark = pytest.mark.postgres


def _candidate_set() -> dict:
    return build_candidate_set(
        [
            build_registered_semantic_candidate(
                intent_key="CONFIGURE_OWNER_REPLY_GRACE",
                parameters={"seconds": 30},
                confidence="high",
            ),
            build_registered_semantic_candidate(
                intent_key="ONE_SHOT_REPLY_DELAY",
                parameters={"seconds": 30},
                confidence="high",
            ),
        ],
        semantic_registry_version=OWNER_CONTROL_SEMANTIC_REGISTRY_VERSION,
    )


def _semantic_result(text: str) -> OwnerControlParseResult:
    if text == "retorne em 30 segundos":
        return OwnerControlParseResult(
            OwnerControlParseStatus.REJECTED,
            reason_code="CONTROL_COMMAND_NEEDS_CLARIFICATION",
        )
    return OwnerControlParseResult(
        OwnerControlParseStatus.NOT_CONTROL_COMMAND
    )


def _install_semantic_mocks(monkeypatch) -> None:
    monkeypatch.setattr(settings, "owner_control_semantic_enabled", True)
    monkeypatch.setattr(
        services,
        "interpret_owner_control_semantically",
        _semantic_result,
    )
    monkeypatch.setattr(
        services,
        "interpret_owner_control_candidates",
        lambda text: (
            _candidate_set()
            if text == "retorne em 30 segundos"
            else None
        ),
    )


def _event(
    context: dict[str, str],
    event_id: str,
    text: str,
    *,
    offset_seconds: int = 0,
) -> NormalizedInboundEvent:
    stamp = now_utc() + timedelta(seconds=offset_seconds)
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
            "source_account": "default",
            "conversation_key": (
                f"wwebjs:{context['owner_external_id']}"
            ),
            "conversation_state": "READY",
        },
    )


def _start_pending(Session, context, suffix: str) -> str:
    with Session() as session:
        services.receive_normalized_inbound_event(
            session,
            _event(
                context,
                f"clarification-source-{suffix}",
                "retorne em 30 segundos",
            ),
        )
        pending_id = session.scalar(
            select(PendingIntentRow.id).where(
                PendingIntentRow.tenant_id == context["tenant_id"]
            )
        )
        assert pending_id is not None
        session.commit()
        return pending_id


def test_postgres_v1c_roundtrip_executes_available_candidate(
    Session,
    monkeypatch,
):
    suffix = uuid.uuid4().hex[:10]
    context = _install(Session, suffix)
    _install_semantic_mocks(monkeypatch)
    pending_id = _start_pending(Session, context, suffix)

    with Session() as session:
        services.receive_normalized_inbound_event(
            session,
            _event(
                context,
                f"clarification-first-{suffix}",
                "1",
                offset_seconds=10,
            ),
        )
        session.commit()

    with Session() as session:
        pending = session.get(PendingIntentRow, pending_id)
        control = session.scalar(
            select(OwnerOperationalControlRow).where(
                OwnerOperationalControlRow.tenant_id
                == context["tenant_id"]
            )
        )
        assert pending is not None
        assert pending.state == "RESOLVED"
        assert control is not None
        assert control.integer_value == 30


def test_postgres_v1c_bare_yes_keeps_a_b_pending(
    Session,
    monkeypatch,
):
    suffix = uuid.uuid4().hex[:10]
    context = _install(Session, suffix)
    _install_semantic_mocks(monkeypatch)
    pending_id = _start_pending(Session, context, suffix)

    with Session() as session:
        services.receive_normalized_inbound_event(
            session,
            _event(
                context,
                f"clarification-yes-{suffix}",
                "sim",
                offset_seconds=10,
            ),
        )
        session.commit()

    with Session() as session:
        pending = session.get(PendingIntentRow, pending_id)
        assert pending is not None
        assert pending.state == "PENDING"
        assert session.scalar(
            select(OwnerOperationalControlRow).where(
                OwnerOperationalControlRow.tenant_id
                == context["tenant_id"]
            )
        ) is None


def test_postgres_v1c_unavailable_candidate_never_executes(
    Session,
    monkeypatch,
):
    suffix = uuid.uuid4().hex[:10]
    context = _install(Session, suffix)
    _install_semantic_mocks(monkeypatch)
    pending_id = _start_pending(Session, context, suffix)

    with Session() as session:
        services.receive_normalized_inbound_event(
            session,
            _event(
                context,
                f"clarification-second-{suffix}",
                "2",
                offset_seconds=10,
            ),
        )
        session.commit()

    with Session() as session:
        pending = session.get(PendingIntentRow, pending_id)
        assert pending is not None
        assert pending.state == "RESOLVED"
        assert session.scalar(
            select(OwnerOperationalControlRow).where(
                OwnerOperationalControlRow.tenant_id
                == context["tenant_id"]
            )
        ) is None
