from __future__ import annotations

import pytest
from sqlalchemy import select

from attention_router.application import services
from attention_router.application.user_idiolect_preference import (
    parse_user_style_preference,
)
from attention_router.application.user_idiolect_style import (
    build_response_style_profile,
)
from attention_router.infrastructure.models import FactRow, OutboxMessageRow
from tests.test_owner_control import _install_owner_and_grace, _owner_event


@pytest.mark.parametrize(
    ("text", "dimension", "value"),
    [
        ("responde mais curto", "response_length", "short"),
        ("Quero respostas mais curtas!", "response_length", "short"),
        ("fala mais técnico comigo", "technical_depth", "high"),
        ("pode ser mais técnico", "technical_depth", "high"),
        ("fala menos técnico comigo", "technical_depth", "low"),
        ("pode ser mais informal", "formality", "low"),
        ("fala mais formal comigo", "formality", "high"),
        ("responde mais direto", "directness", "high"),
        ("fala menos direto comigo", "directness", "low"),
        ("pode usar um pouco de humor", "humor", "occasional"),
        ("sem humor", "humor", "none"),
    ],
)
def test_closed_live_preference_parser(text, dimension, value):
    parsed = parse_user_style_preference(text)

    assert parsed is not None
    assert parsed.dimension == dimension
    assert parsed.value == value


@pytest.mark.parametrize(
    "text",
    [
        "hoje a resposta foi curta",
        "ele fala muito técnico",
        "informalmente falando isso ficou bom",
        "será que respostas curtas são melhores?",
    ],
)
def test_discussion_does_not_become_style_preference(text):
    assert parse_user_style_preference(text) is None


def test_authenticated_owner_preference_is_persisted_and_consumed_live(
    session,
):
    _install_owner_and_grace(session)

    result = services.receive_normalized_inbound_event(
        session,
        _owner_event(
            "style-pref-short",
            "responde mais curto",
        ),
    )

    fact = session.scalar(
        select(FactRow).where(
            FactRow.source_ref == result["inbound_event_id"],
            FactRow.predicate == "communication.preference.response_length",
        )
    )
    outbox = session.scalar(
        select(OutboxMessageRow).where(
            OutboxMessageRow.interaction_id == result["id"]
        )
    )

    assert fact is not None
    assert fact.fact_class == "USER_DECLARED_COMMUNICATION_PREFERENCE"
    assert fact.source_type == "USER_DECLARATION"
    assert fact.value_json["value"] == "short"
    assert fact.value_json["direction"] == "ANDY_TO_USER_PREFERENCE"
    assert fact.value_json["evidence_class"] == "USER_DECLARED"
    assert fact.value_json["reuse_policy"] == "EXPLICIT_REUSE"
    assert outbox is not None
    assert outbox.payload["text"] == "Entendido. Vou responder mais curto."

    profile = build_response_style_profile(
        session,
        tenant_id=fact.tenant_id,
        actor_key=fact.subject_id,
    )
    assert profile.response_length == "short"
    assert profile.adaptation_applied is True


def test_new_explicit_preference_supersedes_previous_dimension(session):
    _install_owner_and_grace(session)

    first_result = services.receive_normalized_inbound_event(
        session,
        _owner_event(
            "style-pref-short-first",
            "responde mais curto",
        ),
    )
    second_result = services.receive_normalized_inbound_event(
        session,
        _owner_event(
            "style-pref-long-second",
            "responde mais detalhado",
        ),
    )

    first = session.scalar(
        select(FactRow).where(
            FactRow.source_ref == first_result["inbound_event_id"]
        )
    )
    second = session.scalar(
        select(FactRow).where(
            FactRow.source_ref == second_result["inbound_event_id"]
        )
    )

    assert first is not None
    assert second is not None
    assert second.predicate == first.predicate
    assert second.value_json["value"] == "long"
    assert second.supersedes_fact_id == first.id

    profile = build_response_style_profile(
        session,
        tenant_id=second.tenant_id,
        actor_key=second.subject_id,
    )
    assert profile.response_length == "long"
    assert "response_length" not in profile.conflict_dimensions


def test_unrelated_owner_message_stays_outside_preference_path(session):
    _install_owner_and_grace(session)

    result = services.receive_normalized_inbound_event(
        session,
        _owner_event(
            "style-pref-unrelated",
            "hoje a resposta foi curta",
        ),
    )

    assert result["event_type"] != "OWNER_CONTROL_COMMAND"
    assert session.scalar(
        select(FactRow).where(
            FactRow.fact_class == "USER_DECLARED_COMMUNICATION_PREFERENCE"
        )
    ) is None



def test_repeating_same_active_preference_is_idempotent(session):
    _install_owner_and_grace(session)

    first_result = services.receive_normalized_inbound_event(
        session,
        _owner_event(
            "style-pref-direct-first",
            "responde mais direto",
        ),
    )
    second_result = services.receive_normalized_inbound_event(
        session,
        _owner_event(
            "style-pref-direct-repeat",
            "responde mais direto",
        ),
    )

    facts = session.scalars(
        select(FactRow).where(
            FactRow.predicate == "communication.preference.directness",
            FactRow.fact_class == "USER_DECLARED_COMMUNICATION_PREFERENCE",
        )
    ).all()
    second_outbox = session.scalar(
        select(OutboxMessageRow).where(
            OutboxMessageRow.interaction_id == second_result["id"]
        )
    )

    assert len(facts) == 1
    assert facts[0].source_ref == first_result["inbound_event_id"]
    assert facts[0].value_json["value"] == "high"
    assert second_outbox is not None
    assert second_outbox.payload["text"] == "Entendido. Vou ser mais direto."

    profile = build_response_style_profile(
        session,
        tenant_id=facts[0].tenant_id,
        actor_key=facts[0].subject_id,
    )
    assert profile.directness == "high"
    assert "directness" not in profile.conflict_dimensions


def test_humor_preference_is_consumed_by_v1k_profile(session):
    _install_owner_and_grace(session)

    result = services.receive_normalized_inbound_event(
        session,
        _owner_event(
            "style-pref-humor",
            "pode usar um pouco de humor",
        ),
    )

    fact = session.scalar(
        select(FactRow).where(
            FactRow.source_ref == result["inbound_event_id"],
            FactRow.predicate == "communication.preference.humor",
        )
    )
    assert fact is not None
    assert fact.value_json["value"] == "occasional"

    profile = build_response_style_profile(
        session,
        tenant_id=fact.tenant_id,
        actor_key=fact.subject_id,
    )
    assert profile.humor == "occasional"
    assert profile.adaptation_applied is True


def test_humor_correction_supersedes_previous_value(session):
    _install_owner_and_grace(session)

    first_result = services.receive_normalized_inbound_event(
        session,
        _owner_event(
            "style-pref-humor-on",
            "pode usar um pouco de humor",
        ),
    )
    second_result = services.receive_normalized_inbound_event(
        session,
        _owner_event(
            "style-pref-humor-off",
            "sem humor",
        ),
    )

    first = session.scalar(
        select(FactRow).where(
            FactRow.source_ref == first_result["inbound_event_id"]
        )
    )
    second = session.scalar(
        select(FactRow).where(
            FactRow.source_ref == second_result["inbound_event_id"]
        )
    )

    assert first is not None
    assert second is not None
    assert second.value_json["value"] == "none"
    assert second.supersedes_fact_id == first.id

    profile = build_response_style_profile(
        session,
        tenant_id=second.tenant_id,
        actor_key=second.subject_id,
    )
    assert profile.humor == "none"
