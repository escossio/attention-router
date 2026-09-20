from __future__ import annotations

from datetime import timedelta

from attention_router.application.user_idiolect_style import (
    build_response_style_profile,
)
from attention_router.core.entities import FactClass
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    FactRow,
    MemoryActorRow,
    MemoryClaimRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding


ACTOR = "style-actor"


def _install_actor(session, external: str = "style-external") -> None:
    upsert_actor_binding(
        session,
        "test",
        external,
        ACTOR,
        "owner",
        metadata={"owner": True},
    )


def _explicit_preference(
    session,
    *,
    dimension: str,
    value: str,
    valid_until=None,
    supersedes_fact_id: str | None = None,
) -> FactRow:
    stamp = now_utc()
    row = FactRow(
        id=new_id(),
        tenant_id=DEFAULT_TENANT_ID,
        subject_type="ACTOR",
        subject_id=ACTOR,
        predicate=f"communication.preference.{dimension}",
        value_json={
            "value": value,
            "direction": "ANDY_TO_USER_PREFERENCE",
            "evidence_class": "USER_DECLARED",
            "reuse_policy": "EXPLICIT_REUSE",
        },
        value_ref=None,
        fact_class=FactClass.USER_DECLARED_COMMUNICATION_PREFERENCE.value,
        source_type="USER_DECLARATION",
        source_ref=f"declaration-{new_id()}",
        confidence=1.0,
        observed_at=stamp,
        valid_from=stamp - timedelta(seconds=1),
        valid_until=valid_until,
        supersedes_fact_id=supersedes_fact_id,
        metadata_json={"sensitivity": "PRIVATE"},
        created_at=stamp,
    )
    session.add(row)
    session.flush()
    return row


def _observed_style_claim(
    session,
    *,
    predicate: str,
    reuse_policy: str,
    style_dimension: str | None = None,
    style_value: str | None = None,
    external_actor: str = "style-external",
    valid_until=None,
) -> MemoryClaimRow:
    stamp = now_utc()
    actor = session.query(MemoryActorRow).filter_by(
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=external_actor,
    ).one_or_none()
    if actor is None:
        actor = MemoryActorRow(
            id=new_id(),
            tenant_id=DEFAULT_TENANT_ID,
            actor_key=external_actor,
            metadata_json={},
            created_at=stamp,
            updated_at=stamp,
        )
        session.add(actor)
        session.flush()

    value = {
        "signal": predicate.rsplit(".", 1)[-1].upper(),
        "direction": "USER_TO_ANDY_LANGUAGE",
        "evidence_class": "OBSERVED",
        "reuse_policy": reuse_policy,
        "generalization_scope": "PERSON",
        "generalization_confidence": 0.25,
    }
    if style_dimension is not None:
        value["style_dimension"] = style_dimension
    if style_value is not None:
        value["style_value"] = style_value

    row = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=actor.id,
        subject_entity_id=None,
        predicate=predicate,
        object_type="JSON",
        object_text=None,
        object_actor_id=None,
        object_entity_id=None,
        object_json=value,
        context={"promotion_rule": "RECURRENCE_V1"},
        confidence=0.70,
        sensitivity_class="NORMAL",
        source_quality="REPEATED_OBSERVATION",
        valid_from=stamp - timedelta(days=1),
        valid_until=valid_until or stamp + timedelta(days=30),
        status="ACTIVE",
        staleness_class="PERISHABLE",
        supersedes_claim_id=None,
        conflict_group_id=None,
        first_observed_at=stamp - timedelta(days=3),
        last_observed_at=stamp,
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    session.flush()
    return row


def test_explicit_user_preference_changes_only_declared_style_dimension(session):
    _install_actor(session)
    _explicit_preference(
        session,
        dimension="response_length",
        value="short",
    )

    profile = build_response_style_profile(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
    )

    assert profile.response_length == "short"
    assert profile.directness == "neutral"
    assert profile.formality == "neutral"
    assert profile.technical_depth == "medium"
    assert profile.humor == "neutral"
    assert profile.adaptation_applied is True
    assert dict(profile.sources)["response_length"] == "EXPLICIT_USER_PREFERENCE"
    assert profile.explicit_preference_count == 1


def test_interpret_only_profanity_observation_cannot_mutate_response_style(session):
    _install_actor(session)
    _observed_style_claim(
        session,
        predicate="communication.observed.profanity_tolerance",
        reuse_policy="INTERPRET_ONLY",
    )

    profile = build_response_style_profile(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
    )

    assert profile.adaptation_applied is False
    assert profile.formality == "neutral"
    assert profile.humor == "neutral"
    assert profile.observed_style_signal_count == 0
    assert set(dict(profile.sources).values()) == {"DEFAULT"}


def test_repeated_style_signal_can_adjust_only_abstract_dimension(session):
    _install_actor(session)
    _observed_style_claim(
        session,
        predicate="communication.observed.humor",
        reuse_policy="STYLE_SIGNAL",
        style_dimension="humor",
        style_value="occasional",
    )

    profile = build_response_style_profile(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
    )

    assert profile.humor == "occasional"
    assert profile.formality == "neutral"
    assert profile.adaptation_applied is True
    assert dict(profile.sources)["humor"] == "REPEATED_OBSERVED_STYLE_SIGNAL"
    assert profile.observed_style_signal_count == 1
    payload = profile.prompt_payload()
    assert "no_phrase_mimicry" in payload["constraints"]
    assert "no_profanity_inference" in payload["constraints"]


def test_explicit_preference_outranks_observed_style_signal(session):
    _install_actor(session)
    _observed_style_claim(
        session,
        predicate="communication.observed.directness",
        reuse_policy="STYLE_SIGNAL",
        style_dimension="directness",
        style_value="high",
    )
    _explicit_preference(
        session,
        dimension="directness",
        value="low",
    )

    profile = build_response_style_profile(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
    )

    assert profile.directness == "low"
    assert dict(profile.sources)["directness"] == "EXPLICIT_USER_PREFERENCE"
    assert profile.explicit_preference_count == 1
    assert profile.observed_style_signal_count == 1


def test_conflicting_explicit_preferences_fail_closed_to_default(session):
    _install_actor(session)
    _explicit_preference(session, dimension="formality", value="low")
    _explicit_preference(session, dimension="formality", value="high")

    profile = build_response_style_profile(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
    )

    assert profile.formality == "neutral"
    assert "formality" in profile.conflict_dimensions
    assert dict(profile.sources)["formality"] == "DEFAULT"


def test_superseded_preference_stays_excluded_when_same_dimension_is_filtered(
    session,
):
    _install_actor(session)
    old = _explicit_preference(
        session,
        dimension="response_length",
        value="short",
    )
    _explicit_preference(
        session,
        dimension="response_length",
        value="long",
        supersedes_fact_id=old.id,
    )

    profile = build_response_style_profile(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
    )

    assert profile.response_length == "long"
    assert "response_length" not in profile.conflict_dimensions


def test_superseded_and_expired_explicit_preferences_are_not_active(session):
    _install_actor(session)
    old = _explicit_preference(
        session,
        dimension="response_length",
        value="long",
    )
    _explicit_preference(
        session,
        dimension="response_length",
        value="short",
        supersedes_fact_id=old.id,
    )
    _explicit_preference(
        session,
        dimension="directness",
        value="high",
        valid_until=now_utc() - timedelta(seconds=1),
    )

    profile = build_response_style_profile(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
    )

    assert profile.response_length == "short"
    assert profile.directness == "neutral"



def test_invalid_explicit_style_value_fails_closed_to_default(session):
    _install_actor(session)
    _explicit_preference(
        session,
        dimension="response_length",
        value="ultra_short",
    )

    profile = build_response_style_profile(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
    )

    assert profile.response_length == "medium"
    assert profile.adaptation_applied is False
    assert dict(profile.sources)["response_length"] == "DEFAULT"
