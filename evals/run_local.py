"""Small real-agent eval runner; requires OPENAI_API_KEY and ANDY_AGENT_ENABLED=true."""

import json
import os
from statistics import mean, median
from time import monotonic
from pathlib import Path

from attention_router.application.agents.context import AllowedAgentContext
from attention_router.application.agents.service import AndyAgentError, run_andy
from graders import semantic_case_pass


def main() -> int:
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required")
    cases = [json.loads(line) for line in Path(__file__).with_name("cases.jsonl").read_text().splitlines()]
    results = []
    for case in cases:
        started = monotonic()
        previous = [{"role": "user", "content": item} for item in case.get("turns", [])[:-1]]
        try:
            output = run_andy(AllowedAgentContext(None, None, "autonomy_canary", "REQUIRES_APPROVAL", recent_turns=previous, current_message=case["input"])).output
            passed = semantic_case_pass(output.model_dump(), case["expected"])
            payload = output.model_dump()
        except AndyAgentError as exc:
            passed = bool(case["expected"].get("no_secret")) and "IDENTITY_VIOLATION" in str(exc)
            payload = {"error": str(exc), "conversation_state": "hold"}
        elapsed = (monotonic() - started) * 1000
        results.append({"id": case["id"], "pass": passed, "latency_ms": elapsed, "output": payload})
    target = Path(__file__).with_name("results")
    target.mkdir(exist_ok=True)
    (target / "latest.json").write_text(json.dumps({"cases": results, "summary": {"pass_rate": sum(x["pass"] for x in results) / len(results), "latency_p50_ms": median(x["latency_ms"] for x in results), "latency_mean_ms": mean(x["latency_ms"] for x in results)}}, ensure_ascii=False, indent=2))
    return 0 if all(x["pass"] for x in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
