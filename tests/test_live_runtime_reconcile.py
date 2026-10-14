from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).parents[1]
    / "ops"
    / "provisioning"
    / "live-runtime"
    / "reconcile.py"
)
SPEC = importlib.util.spec_from_file_location("live_runtime_reconcile", MODULE_PATH)
assert SPEC and SPEC.loader
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def test_compose_files_preserve_declared_order(monkeypatch):
    monkeypatch.setenv(
        "ATTENTION_LIVE_WORKER_COMPOSE_FILES",
        "/etc/base.yaml:/etc/otel.yaml:/etc/production.yaml",
    )
    assert runtime.compose_files() == [
        "/etc/base.yaml",
        "/etc/otel.yaml",
        "/etc/production.yaml",
    ]


def test_verify_detects_execution_gate_drift(monkeypatch):
    monkeypatch.setenv("ATTENTION_LIVE_DB_CONTAINER", "db")
    monkeypatch.setenv("ATTENTION_LIVE_WORKER_CONTAINER", "worker")

    def fake_inspect(container, template):
        if "RestartPolicy" in template:
            return "unless-stopped"
        if "Health" in template:
            return "healthy"
        raise AssertionError((container, template))

    monkeypatch.setattr(runtime, "inspect_value", fake_inspect)
    monkeypatch.setattr(
        runtime,
        "container_env",
        lambda _container: {"AGENT_EXECUTION_ENABLED": "false"},
    )

    with pytest.raises(runtime.RuntimeInvariantError, match="AGENT_EXECUTION_ENABLED"):
        runtime.verify()


def test_verify_accepts_expected_runtime(monkeypatch):
    monkeypatch.setenv("ATTENTION_LIVE_DB_CONTAINER", "db")
    monkeypatch.setenv("ATTENTION_LIVE_WORKER_CONTAINER", "worker")

    def fake_inspect(_container, template):
        if "RestartPolicy" in template:
            return "unless-stopped"
        if "Health" in template:
            return "healthy"
        raise AssertionError(template)

    monkeypatch.setattr(runtime, "inspect_value", fake_inspect)
    monkeypatch.setattr(
        runtime,
        "container_env",
        lambda _container: {"AGENT_EXECUTION_ENABLED": "true"},
    )

    result = runtime.verify()
    assert result["agent_execution_enabled"] == "true"
    assert result["db_restart"] == "unless-stopped"
    assert result["worker_restart"] == "unless-stopped"
