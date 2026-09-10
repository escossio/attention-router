from scripts import ci_agent
from scripts import ci_copilot_advisor as advisor
from scripts import ci_copilot_patch_trial_entrypoint as entrypoint


def _comment(body: str):
    return {
        "body": body,
        "user": {"type": "Bot"},
    }


def test_advisor_gate_requires_propose_fix_high_none_for_same_head(monkeypatch):
    head = "a" * 40
    body = (
        f"{advisor.ADVISOR_MARKER}\n"
        "### Copilot Advisor V1 🧪 DRY-RUN · PROPOSE_FIX\n\n"
        f"PR: `#41` · head: `{head[:12]}`\n\n"
        "**Confiança:** `HIGH`\n"
        "**Risk flags:** `NONE`\n"
    )
    monkeypatch.setattr(ci_agent, "_request_json", lambda *args, **kwargs: [_comment(body)])

    assert entrypoint.advisor_gate_reason("escossio/attention-router", 41, head, token="token") is None


def test_advisor_gate_fails_closed_for_missing_stale_or_risky_evidence(monkeypatch):
    head = "b" * 40

    monkeypatch.setattr(ci_agent, "_request_json", lambda *args, **kwargs: [])
    assert entrypoint.advisor_gate_reason("escossio/attention-router", 41, head, token="token") == "ADVISOR_EVIDENCE_MISSING"

    stale = (
        f"{advisor.ADVISOR_MARKER}\n"
        "### Copilot Advisor V1 🧪 DRY-RUN · PROPOSE_FIX\n"
        "head: `cccccccccccc`\n"
        "**Confiança:** `HIGH`\n"
        "**Risk flags:** `NONE`\n"
    )
    monkeypatch.setattr(ci_agent, "_request_json", lambda *args, **kwargs: [_comment(stale)])
    assert entrypoint.advisor_gate_reason("escossio/attention-router", 41, head, token="token") == "ADVISOR_EVIDENCE_STALE_HEAD"

    risky = (
        f"{advisor.ADVISOR_MARKER}\n"
        "### Copilot Advisor V1 🧪 DRY-RUN · PROPOSE_FIX\n"
        f"head: `{head[:12]}`\n"
        "**Confiança:** `HIGH`\n"
        "**Risk flags:** `AMBIGUOUS`\n"
    )
    monkeypatch.setattr(ci_agent, "_request_json", lambda *args, **kwargs: [_comment(risky)])
    assert entrypoint.advisor_gate_reason("escossio/attention-router", 41, head, token="token") == "ADVISOR_RISK_NOT_NONE"
