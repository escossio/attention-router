from dataclasses import dataclass
import re
from time import monotonic

from pydantic import ValidationError

from attention_router.application.agents.andy_agent import build_andy_agent
from attention_router.application.agents.context import AllowedAgentContext
from attention_router.application.agents.output import AndyAgentOutput
from attention_router.config import settings


class AndyAgentError(RuntimeError):
    pass


_IDENTITY_CLAIM_RE = re.compile(
    r"\b(?:(?P<negated>(?:(?:eu)\s+)?(?:não|nao)\s+)|(?P<subject>eu\s+)?)"
    r"sou\s+(?:o\s+alex|humana)\b"
)


def _contains_forbidden_identity_claim(text: str) -> bool:
    normalized = " ".join(text.casefold().split())
    return any(match.group("negated") is None for match in _IDENTITY_CLAIM_RE.finditer(normalized))


@dataclass(frozen=True)
class AndyAgentResult:
    output: AndyAgentOutput
    duration_ms: float
    turn_count: int
    tool_call_count: int


def validate_output(output: AndyAgentOutput) -> AndyAgentOutput:
    if output.conversation_state == "answer" and not output.response_text.strip():
        raise AndyAgentError("AGENT_OUTPUT_EMPTY_RESPONSE")
    lowered = output.response_text.casefold()
    if _contains_forbidden_identity_claim(output.response_text):
        raise AndyAgentError("AGENT_OUTPUT_IDENTITY_VIOLATION")
    if any(marker in lowered for marker in ("já avisei", "já enviei", "já registrei", "já pedi para o alex")):
        raise AndyAgentError("AGENT_OUTPUT_UNEXECUTED_ACTION_CLAIM")
    if output.conversation_state == "hold" and output.response_text.strip():
        raise AndyAgentError("AGENT_OUTPUT_HOLD_WITH_RESPONSE")
    return output


def run_andy(context: AllowedAgentContext) -> AndyAgentResult:
    started = monotonic()
    try:
        from agents import Runner

        result = Runner.run_sync(
            build_andy_agent(),
            input=[{"role": "user", "content": str(context.prompt_payload())}],
            max_turns=settings.andy_agent_max_turns,
        )
        output = validate_output(result.final_output)
    except (AndyAgentError, ValidationError) as exc:
        raise AndyAgentError(str(exc)) from exc
    except Exception as exc:
        raise AndyAgentError("AGENT_RUN_FAILED") from exc
    return AndyAgentResult(
        output=output,
        duration_ms=(monotonic() - started) * 1000,
        turn_count=1,
        tool_call_count=0,
    )
