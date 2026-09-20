from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from attention_router.application import services
from attention_router.application.user_idiolect_style import (
    build_response_style_profile,
)
from attention_router.application.user_idiolect_style_preference import (
    parse_explicit_style_preference,
)
from attention_router.core.entities import FactClass
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import FactRow, OutboxMessageRow
from tests.test_owner_control import (
    OWNER_ACTOR_ID,
    _install_owner_and_grace,
)
from tests.test_owner_control_clarification import _event


def _outbox(session, interaction_id: str) -> OutboxMessageRow:
    row = session.scalar(
        select(OutboxMessageRow).where(
            OutboxMessageRow.interaction_id == interaction_id
        )
    )
    assert row is not None
    return row


def test_parser_accepts_bounded_explicit_style_declarations():
    assert parse_explicit_style_preference("responde mais curto").dimension == (
        "response_length"
    )
    assert parse_explicit_style_preference("fala mais técnico comigo").value == "high"
    assert parse_explicit_style_preference("pode ser mais informal").value == "low"
    assert parse_explicit_style_preference("pode usar um pouco de humor").value == (
        "occasional"
    )


def test_parser_rejects_question_and_unbounded_style_language():
    assert parse_explicit_style_preference("você pode responder mais curto?") is None
    assert parse_explicit_style_preference("fala igual a mim") is None
    assert parse_explicit_style_preference("pode xingar comigo") is None
    assert parse_explicit_style_preference("me chama de meu anjo lindo") is None


def test_authenticated_owner_declaration_persists_fact_and_confirms(session):
    _install_owner_and_grace(session)

    result = services.receive_normalized_inbound_event(
        session,
        _event("style-short-1", "responde mais curto"),
    )

    fact = session.scalar(
        select(FactRow).where(
            FactRow.fact_class
            == FactClass.USER_DECLARED_COMMUNICATION_PREFERENCE.value,
            FactRow.predicate == "communication.preference.response_length",
        )
    )
    assert fact is not None
    assert fact.subject_id == OWNER_ACTOR_ID
    assert fact.value_json["value"] == "short"
    assert fact.value_json["direction"] == "ANDY_TO_USER_PREFERENCE"
    assert fact.value_json["evidence_class"] == "USER_DECLARED"
    assert fact.value_json["reuse_policy"] == "EXPLICIT_REUSE"
    assert fact.value_json["generalization_scope"] == "PERSON"

    outbox = _outbox(session, result["id"])
    assert outbox.payload["text"] == "Vou responder de forma mais curta."

    profile = build_response_style_profile(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=OWNER_ACTOR_ID,
    )
    assert profile.response_length == "short"
    assert profile.adaptation_applied is True


def test_repeated_same_declaration_is_idempotent_at_active_preference_level(session):
    _install_owner_and_grace(session)

    services.receive_normalized_inbound_event(
        session,
        _event("style-short-repeat-1", "responde mais curto"),
    )
    result = services.receive_normalized_inbound_event(
        session,
        _event(
            "style-short-repeat-2",
            "responde mais curto",
            offset_seconds=10,
        ),
    )

    assert session.scalar(
        select(func.count())
        .select_from(FactRow)
        .where(
            FactRow.fact_class
            == FactClass.USER_DECLARED_COMMUNICATION_PREFERENCE.value,
            FactRow.predicate == "communication.preference.response_length",
        )
    ) == 1
    assert "já está ativa" in _outbox(session, result["id"]).payload["text"]


def test_explicit_correction_supersedes_prior_style_preference(session):
    _install_owner_and_grace(session)

    services.receive_normalized_inbound_event(
        session,
        _event("style-short-correction-1", "responde mais curto"),
    )
    first = session.scalar(
        select(FactRow).where(
            FactRow.predicate == "communication.preference.response_length"
        )
    )
    assert first is not None

    services.receive_normalized_inbound_event(
        session,
        _event(
            "style-long-correction-2",
            "responde mais detalhado",
            offset_seconds=10,
        ),
    )
    rows = session.scalars(
        select(FactRow)
        .where(
            FactRow.predicate == "communication.preference.response_length"
        )
        .order_by(FactRow.created_at)
    ).all()

    assert len(rows) == 2
    assert rows[1].supersedes_fact_id == first.id
    profile = build_response_style_profile(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=OWNER_ACTOR_ID,
    )
    assert profile.response_length == "long"
    assert "response_length" not in profile.conflict_dimensions


def test_style_declaration_does_not_enter_owner_effect_executor(session, monkeypatch):
    _install_owner_and_grace(session)
    calls = []

    monkeypatch.setattr(
        services,
        "dispatch_owner_control_signal",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    services.receive_normalized_inbound_event(
        session,
        _event("style-no-effect-executor", "fala mais técnico comigo"),
    )

    assert calls == []


def test_style_declaration_requires_authenticated_owner_provenance(session):
    _install_owner_and_grace(session)
    event = _event("style-unauthenticated", "responde mais curto")
    event = event.model_copy(update={"owner_authenticated": False})

    services.receive_normalized_inbound_event(session, event)

    assert session.scalar(
        select(func.count())
        .select_from(FactRow)
        .where(
            FactRow.fact_class
            == FactClass.USER_DECLARED_COMMUNICATION_PREFERENCE.value
        )
    ) == 0
