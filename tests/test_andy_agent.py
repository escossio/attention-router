from attention_router.application.agents.output import AndyAgentOutput
from attention_router.application.agents.service import AndyAgentError, validate_output


def test_structured_answer_requires_response():
    output = AndyAgentOutput(
        response_text="",
        intent="identity_question",
        objective="identity/name",
        reason_code="test",
        confidence="high",
        conversation_state="answer",
    )
    try:
        validate_output(output)
    except AndyAgentError as exc:
        assert str(exc) == "AGENT_OUTPUT_EMPTY_RESPONSE"
    else:
        raise AssertionError("empty answer must fail closed")


def test_structured_action_is_proposal_only():
    output = AndyAgentOutput(
        response_text="Vou registrar o pedido para análise.",
        intent="callback_request",
        objective="callback/request",
        reason_code="action_proposed",
        confidence="medium",
        requested_actions=[{"action_type": "request_callback", "user_requested": True}],
        conversation_state="action_requested",
    )
    assert validate_output(output).requested_actions[0].action_type == "request_callback"


def test_structured_capability_request_is_open_ended_and_proposal_only():
    output = AndyAgentOutput(
        response_text="Posso verificar se essa capability está disponível.",
        intent="capability_request",
        objective="environment/temperature",
        reason_code="capability_proposed",
        confidence="high",
        requested_capabilities=[{
            "capability": "environment.temperature",
            "parameters": {"unit": "celsius"},
            "user_requested": True,
            "confidence": "high",
        }],
        conversation_state="action_requested",
    )
    request = validate_output(output).requested_capabilities[0]
    assert request.capability == "environment.temperature"
    assert request.user_requested is True


def _answer(text: str) -> AndyAgentOutput:
    return AndyAgentOutput(
        response_text=text,
        intent="identity_question",
        objective="identity/name",
        reason_code="test",
        confidence="high",
        conversation_state="answer",
    )


def test_negated_identity_statements_are_allowed():
    for text in (
        "Não sou o Alex.",
        "Eu não sou o Alex.",
        "Não sou humana.",
        "Sou a Andy, assistente virtual do Alex.",
        "Posso pedir essa informação ao Alex.",
        "Alex não está disponível agora.",
    ):
        assert validate_output(_answer(text)).response_text == text


def test_positive_identity_statements_remain_blocked():
    for text in (
        "Sou o Alex.",
        "Eu sou o Alex.",
        "Oi, sou o Alex.",
        "Sou humana.",
        "Eu sou humana.",
    ):
        try:
            validate_output(_answer(text))
        except AndyAgentError as exc:
            assert str(exc) == "AGENT_OUTPUT_IDENTITY_VIOLATION"
        else:
            raise AssertionError("positive identity claim must fail closed")
