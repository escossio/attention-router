from __future__ import annotations

import importlib.util
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "ops/provisioning/andy-ops-panel"
PRIVATE_IPV4 = re.compile(
    r"(?:192\.168\.|10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)"
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_transport_observability_is_default_off(monkeypatch):
    monkeypatch.delenv("ANDY_OPS_TRANSPORT_OBSERVABILITY_ENABLED", raising=False)
    module = load_module("transport_observability_test", PANEL / "transport_observability.py")
    payload = module.sample_transport_observability()
    assert payload["enabled"] is False
    assert payload["derived"]["severity"] == "DISABLED"
    assert payload["timeline"] == []


def test_message_tracing_is_default_off_and_has_no_runtime_container_default(monkeypatch):
    monkeypatch.delenv("ANDY_OPS_MESSAGE_TRACING_ENABLED", raising=False)
    monkeypatch.delenv("ANDY_OPS_TRACE_DB_CONTAINER", raising=False)
    module = load_module("message_tracing_test", PANEL / "message_tracing.py")
    payload = module.sample_message_traces()
    assert payload["enabled"] is False
    assert payload["ok"] is True
    assert payload["error"] is None
    assert payload["traces"] == []
    source = (PANEL / "message_tracing.py").read_text()
    assert "attention-router-live-flow-stage4-db-1" not in source
    assert "o.destination" not in source
    assert "outbound.get" + "('destination')" not in source


def test_public_panel_sources_have_no_private_ipv4_literals():
    for name in (
        "server.py",
        "transport_observability.py",
        "message_tracing.py",
        "app.js",
        "index.html",
        "styles.css",
        "andy-ops-panel.env.example",
    ):
        content = (PANEL / name).read_text()
        assert not PRIVATE_IPV4.search(content), name


def test_frontend_and_server_expose_optional_transport_and_trace_surfaces():
    server = (PANEL / "server.py").read_text()
    app = (PANEL / "app.js").read_text()
    assert '"transport_observability": transport_observability' in server
    assert '"message_traces": message_traces' in server
    assert "renderTransport(payload.transport_observability || {})" in app
    assert "renderMessageTraces(payload.message_traces || {})" in app
    assert "tracePayload.enabled === false" in app
    assert "obs.enabled === false" in app
