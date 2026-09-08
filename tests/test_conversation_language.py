import pytest

from attention_router.application.agents.context import AllowedAgentContext
from attention_router.application.conversation_language import resolve_conversation_locale


@pytest.mark.parametrize(
    ("message", "response", "expected"),
    [
        ("Responda apenas com o resultado: dez mais dez.", "vinte", "pt-BR"),
        ("Responda apenas com o resultado: dez mais dez.", "20", "pt-BR"),
        ("Answer only with the result: ten plus ten.", "twenty", "en"),
        ("Answer only with the result: ten plus ten.", "20", "en"),
    ],
)
def test_text_and_numeric_responses_keep_current_user_language(message, response, expected):
    resolved = resolve_conversation_locale(message, [])
    assert resolved.locale == expected
    assert response


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Agora responda em inglês: quanto é vinte mais zero?", "en"),
        ("Now answer in Portuguese: what is twenty plus zero?", "pt-BR"),
    ],
)
def test_explicit_language_switch_wins_over_mixed_message(message, expected):
    assert resolve_conversation_locale(message, []).locale == expected


def test_neutral_current_message_preserves_recent_conversation_language():
    history = [{"role": "user", "content": "Quanto é dez mais dez?"}]
    assert resolve_conversation_locale("20?", history).locale == "pt-BR"


def test_neutral_response_cannot_cause_spontaneous_switch():
    context = AllowedAgentContext(
        actor_id=None,
        binding_id=None,
        audience="contact",
        policy_summary="safe",
        current_message="Qual é vinte mais zero?",
    )
    payload = context.prompt_payload()
    assert payload["response_locale"] == "pt-BR"
    assert payload["response_locale_source"] == "current_message"
