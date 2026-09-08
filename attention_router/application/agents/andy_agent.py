from pathlib import Path

from attention_router.config import settings
from attention_router.application.agents.output import AndyAgentOutput


def build_andy_agent():
    from agents import Agent, AgentOutputSchema

    instructions = Path(__file__).with_name("instructions.md").read_text(encoding="utf-8")
    return Agent(
        name="ANDY",
        instructions=instructions,
        model=settings.andy_agent_model,
        output_type=AgentOutputSchema(AndyAgentOutput, strict_json_schema=False),
    )
