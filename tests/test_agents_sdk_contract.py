import json

from agents import Model, ModelResponse, RunConfig, Runner, Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from attention_router.application.agents.andy_agent import build_andy_agent
from attention_router.application.agents.output import AndyAgentOutput


class _OfflineStructuredModel(Model):
    def __init__(self) -> None:
        self.calls = 0
        self.saw_output_schema = False
        self.system_instructions: str | None = None

    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        *,
        previous_response_id,
        conversation_id,
        prompt,
    ) -> ModelResponse:
        self.calls += 1
        self.saw_output_schema = output_schema is not None
        self.system_instructions = system_instructions
        assert tools == []
        assert handoffs == []

        payload = {
            "response_text": "Andy: contrato offline OK.",
            "intent": "contract_test",
            "objective": "sdk/compatibility",
            "reason_code": "OFFLINE_SMOKE",
            "confidence": "high",
            "conversation_state": "answer",
        }
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="offline-message",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(
                            type="output_text",
                            text=json.dumps(payload),
                            annotations=[],
                            logprobs=[],
                        )
                    ],
                )
            ],
            usage=Usage(),
            response_id="offline-response",
        )

    def stream_response(self, *args, **kwargs):
        raise AssertionError("streaming is outside this offline contract smoke test")


def test_agents_sdk_structured_runner_contract_is_offline_compatible(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    model = _OfflineStructuredModel()
    agent = build_andy_agent()
    agent.model = model

    result = Runner.run_sync(
        agent,
        "offline SDK contract probe",
        max_turns=1,
        run_config=RunConfig(tracing_disabled=True),
    )

    assert model.calls == 1
    assert model.saw_output_schema is True
    assert model.system_instructions
    assert isinstance(result.final_output, AndyAgentOutput)
    assert result.final_output.response_text == "Andy: contrato offline OK."
    assert result.final_output.intent == "contract_test"
    assert result.final_output.conversation_state == "answer"
