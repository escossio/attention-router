from attention_router.application.agents.readiness import get_andy_readiness
from attention_router.config import settings


def _enable_agent(monkeypatch, key):
    monkeypatch.setattr(settings, "andy_agent_enabled", True)
    monkeypatch.setattr(settings, "andy_agent_model", "test-model")
    monkeypatch.setattr(settings, "andy_agent_max_turns", 1)
    monkeypatch.setattr(settings, "openai_api_key", key)
    monkeypatch.setattr(
        "attention_router.application.agents.readiness.importlib.util.find_spec",
        lambda _name: object(),
    )


def test_andy_readiness_fails_closed_when_runtime_disabled(monkeypatch):
    monkeypatch.setattr(settings, "andy_agent_enabled", False)
    monkeypatch.setattr(settings, "andy_behavior_enabled", False)
    result = get_andy_readiness()
    assert result.state == "NOT_READY"
    assert result.reason_code == "ANDY_RUNTIME_DISABLED"


def test_andy_readiness_requires_api_key_when_agent_enabled(monkeypatch):
    _enable_agent(monkeypatch, None)
    result = get_andy_readiness()
    assert result.state == "NOT_READY"
    assert result.reason_code == "ANDY_AGENT_API_KEY_MISSING"


def test_andy_readiness_rejects_blank_api_key(monkeypatch):
    _enable_agent(monkeypatch, " \t\n")
    result = get_andy_readiness()
    assert result.state == "NOT_READY"
    assert result.reason_code == "ANDY_AGENT_API_KEY_MISSING"


def test_andy_readiness_is_ready_with_api_key(monkeypatch):
    _enable_agent(monkeypatch, "present-but-never-used-in-test")
    result = get_andy_readiness()
    assert result.state == "READY"
    assert result.reason_code == "ANDY_AGENT_READY"


def test_behavior_only_path_does_not_require_api_key(monkeypatch):
    monkeypatch.setattr(settings, "andy_agent_enabled", False)
    monkeypatch.setattr(settings, "andy_behavior_enabled", True)
    monkeypatch.setattr(settings, "andy_behavior_canary_binding_id", "binding")
    monkeypatch.setattr(settings, "andy_behavior_profile_path", "config/andy_behavior_profile.json")
    monkeypatch.setattr(settings, "openai_api_key", None)
    result = get_andy_readiness()
    assert result.state == "READY"
    assert result.reason_code == "ANDY_BEHAVIOR_READY"


def test_pipeline_disabled_precedes_missing_api_key(monkeypatch):
    _enable_agent(monkeypatch, None)
    monkeypatch.setattr(settings, "agent_decision_pipeline_enabled", False)
    result = get_andy_readiness()
    assert result.state == "NOT_READY"
    assert result.reason_code == "ANDY_PIPELINE_DISABLED"


def test_agent_gate_stays_closed_without_key_and_does_not_call_agent(monkeypatch):
    _enable_agent(monkeypatch, None)
    monkeypatch.setattr(settings, "agent_decision_pipeline_enabled", True)
    from attention_router.application import decision_pipeline

    calls = []
    monkeypatch.setattr(decision_pipeline, "run_andy", lambda *_args, **_kwargs: calls.append(True))
    gate = decision_pipeline._agent_enabled_for(
        None,
        "unknown",
        None,
        event=None,
        blueprint_configured=True,
    )
    assert gate is False
    assert calls == []
