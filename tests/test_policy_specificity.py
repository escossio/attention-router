from attention_router.domain.policies import resolve_policy
from attention_router.infrastructure.repository import create_policy, list_policies, upsert_actor_binding


def _config(identifier, criteria, *, priority=1, specificity=1):
    return {
        "identifier": identifier,
        "name": identifier,
        "match_criteria": criteria,
        "priority": priority,
        "specificity": specificity,
        "tone": "neutral",
        "initial_wait_seconds": 1,
        "allowed_disclosures": [],
        "allowed_actions": ["request_information"],
        "escalation_steps": ["request_information"],
        "ack_timeout_seconds": 10,
        "repetition_limit": 1,
        "cancellation_conditions": [],
        "completion_conditions": ["safe_completion"],
    }


def test_exact_binding_wins_over_high_priority_generic_fallback(session):
    binding = upsert_actor_binding(
        session, "test", "external-specific", "actor-specific", "known", metadata={"audience": "canary"}
    )
    create_policy(session, "specific-binding", {**_config("specific-binding", {"binding_id": binding.id}, priority=1), "specificity": 1})
    create_policy(session, "generic-high-priority", _config("generic-high-priority", {"default": True}, priority=9999, specificity=9999))
    session.flush()

    resolution = resolve_policy(
        list_policies(session), "actor-specific", "known", None, audience="canary", binding_id=binding.id
    )

    assert resolution.winner.identifier == "specific-binding"
    assert resolution.matched[0]["scope_rank"] > resolution.matched[1]["scope_rank"]


def test_audience_policy_beats_generic_fallback(session):
    create_policy(session, "audience-policy", _config("audience-policy", {"audience": "canary"}, priority=1))
    create_policy(session, "generic-audience-fallback", _config("generic-audience-fallback", {"default": True}, priority=9999))
    session.flush()

    resolution = resolve_policy(list_policies(session), "actor", "known", None, audience="canary")

    assert resolution.winner.identifier == "audience-policy"


def test_generic_fallback_only_when_no_specific_candidate(session):
    create_policy(session, "generic-only", _config("generic-only", {"default": True}))
    session.flush()

    resolution = resolve_policy(list_policies(session), "actor", "known", None, audience="other")

    assert resolution.winner.identifier == "generic-only"


def test_inactive_specific_policy_does_not_win(session):
    binding = upsert_actor_binding(session, "test", "external-inactive", "actor-inactive", "known")
    create_policy(session, "inactive-binding", _config("inactive-binding", {"binding_id": binding.id}, priority=9999))
    from attention_router.infrastructure.repository import deactivate_policy

    deactivate_policy(session, "inactive-binding")
    session.flush()

    resolution = resolve_policy(list_policies(session), "actor-inactive", "known", None, binding_id=binding.id)

    assert resolution.winner.identifier == "desconhecido"


def test_binding_specificity_isolated_to_its_actor(session):
    binding = upsert_actor_binding(session, "test", "external-a", "actor-a", "known")
    create_policy(session, "binding-a", _config("binding-a", {"binding_id": binding.id}, priority=9999))
    session.flush()

    resolution = resolve_policy(list_policies(session), "actor-b", "known", None, binding_id="other-binding")

    assert resolution.winner.identifier == "desconhecido"
