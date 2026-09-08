import pytest

from attention_router.domain.decision import DecisionContext, DecisionEngine


def decision_for(text: str, missing: list[str] | None = None):
    return DecisionEngine().decide(
        DecisionContext(
            event_id="event",
            interaction_id="interaction",
            channel="wwebjs",
            source="wwebjs",
            actor_id="actor",
            actor_binding_id="binding",
            actor_category="family_core",
            actor_metadata={},
            agent_blueprint_id="blueprint",
            agent_blueprint_version=1,
            agent_spec={"missing_information": missing or [], "escalation": []},
            audience="autonomy_canary",
            policy_id="autonomy_canary_v1",
            policy_version_id="policy-version",
            policy_config={"allowed_actions": ["request_information"]},
            autonomy="observe",
            inbound_text=text,
        )
    )


@pytest.mark.parametrize(
    ("text", "objective"),
    [
        ("Qual é o seu nome?", "identity/name"),
        ("Quem fala?", "identity/name"),
        ("Como você se chama?", "identity/name"),
        ("Você é uma IA?", "identity/transparency"),
    ],
)
def test_self_contained_identity_never_requests_context(text, objective):
    result = decision_for(text, ["context", "subject"])

    assert result.intent in {"IDENTITY_QUESTION", "ASSISTANT_NATURE_QUESTION"}
    assert result.objective == objective
    assert result.self_contained is True
    assert result.context_sufficient is True
    assert result.context_requirements == ["context", "subject"]
    assert result.missing_information == []
    assert result.decision_type.value == "RESPOND"
    assert result.recommended_action == "respond"


@pytest.mark.parametrize(
    "text",
    ["Preciso falar com o Alex.", "Preciso falar com o Alex sobre trabalho."],
)
def test_callback_context_requirements_are_preserved(text):
    result = decision_for(text, ["subject"])

    assert result.intent == "GENERAL_CONTEXT_REQUEST"
    assert result.objective is None
    assert result.self_contained is False
    assert result.context_sufficient is False
    assert result.missing_information == ["subject"]
    assert result.decision_type.value == "INSUFFICIENT_CONTEXT"
    assert result.recommended_action == "request_information"
