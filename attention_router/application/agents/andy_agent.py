from pathlib import Path

from attention_router.config import settings
from attention_router.application.agents.output import AndyAgentOutput


def build_andy_agent(*, public_identity: bool = False):
    from agents import Agent, AgentOutputSchema

    instructions = Path(__file__).with_name("instructions.md").read_text(encoding="utf-8")
    if public_identity:
        instructions = instructions.replace(
            "Você é Andy, assistente virtual do Alex.",
            "Você é Andy, assistente virtual nesta conta.",
            1,
        ).replace("Alex", "o proprietário").replace("alex", "proprietário")
    return Agent(
        name="ANDY",
        instructions=instructions,
        model=settings.andy_agent_model,
        output_type=AgentOutputSchema(AndyAgentOutput, strict_json_schema=False),
    )
