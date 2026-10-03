from __future__ import annotations

import importlib.util
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PANEL_ROOT = ROOT / "ops/provisioning/andy-ops-panel"
TRANSPORT = PANEL_ROOT / "transport_observability.py"
TRACING = PANEL_ROOT / "message_tracing.py"
SERVER = PANEL_ROOT / "server.py"
INSTALL = PANEL_ROOT / "install.sh"
PRIVATE_IPV4 = re.compile(
    r"(?:192\.168\.|10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)"
)


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_transport_projection_is_allowlisted():
    module = load(TRANSPORT, "andy_transport_observability_test")
    payload = {
        "ready": True,
        "client_state": "CONNECTED",
        "owner_command_authority_ready": True,
        "owner_identity_verification": "MATCH",
        "inbound_seen_count": 7,
        "owner_phone": "should-not-leak",
        "session_token": "should-not-leak",
        "arbitrary_provider_payload": {"secret": True},
    }

    projected = module._transport_projection(payload)

    assert projected["ready"] is True
    assert projected["client_state"] == "CONNECTED"
    assert projected["owner_command_authority_ready"] is True
    assert projected["inbound_seen_count"] == 7
    assert "owner_phone" not in projected
    assert "session_token" not in projected
    assert "arbitrary_provider_payload" not in projected


def test_message_trace_browser_projection_excludes_direct_identifiers():
    module = load(TRACING, "andy_message_tracing_test")
    row = {
        "id": "evt_private_identifier",
        "correlation_id": "correlation_private_identifier",
        "source": "wwebjs",
        "event_type": "message",
        "status": "PROCESSED",
        "received_at": "2026-10-02T20:00:00+00:00",
        "processed_at": "2026-10-02T20:00:01+00:00",
        "audits": [],
        "queues": [],
        "decisions": [],
        "intents": [],
        "outbox": [
            {
                "status": "DONE",
                "destination": "sensitive-destination",
                "created_at": "2026-10-02T20:00:02+00:00",
                "completed_at": "2026-10-02T20:00:03+00:00",
            }
        ],
    }

    trace = module._build_trace(row)

    assert "correlation_id" not in trace
    assert "inbound_event_id" not in trace
    assert trace["correlation_short"] == "correlation_"
    serialized = repr(trace)
    assert "evt_private_identifier" not in serialized
    assert "sensitive-destination" not in serialized


def test_message_trace_requires_explicit_runtime_db_container():
    source = TRACING.read_text()
    assert "ANDY_OPS_MESSAGE_TRACING_ENABLED" in source
    assert '"false"' in source
    assert 'ANDY_OPS_TRACE_DB_CONTAINER", "").strip()' in source
    assert "TRACE_DB_CONTAINER_NOT_CONFIGURED" in source
    assert "'destination', o.destination" not in source


def test_server_keeps_process_backed_ci_state_and_runtime_observers():
    source = SERVER.read_text()
    assert "from transport_observability import sample_transport_observability" in source
    assert "from message_tracing import sample_message_traces" in source
    assert "_recent_dispatches(active_sha_shorts=active_sha_shorts)" in source
    assert '"transport_observability": transport_observability' in source
    assert '"message_traces": message_traces' in source


def test_installer_materializes_runtime_observability_modules():
    source = INSTALL.read_text()
    assert 'transport_observability.py" "$INSTALL_DIR/transport_observability.py' in source
    assert 'message_tracing.py" "$INSTALL_DIR/message_tracing.py' in source


def test_transport_observability_is_default_off(monkeypatch):
    monkeypatch.delenv("ANDY_OPS_TRANSPORT_OBSERVABILITY_ENABLED", raising=False)
    module = load(TRANSPORT, "andy_transport_observability_default_off_test")
    payload = module.sample_transport_observability()

    assert payload["enabled"] is False
    assert payload["derived"]["severity"] == "DISABLED"
    assert payload["timeline"] == []


def test_message_tracing_is_default_off(monkeypatch):
    monkeypatch.delenv("ANDY_OPS_MESSAGE_TRACING_ENABLED", raising=False)
    monkeypatch.delenv("ANDY_OPS_TRACE_DB_CONTAINER", raising=False)
    module = load(TRACING, "andy_message_tracing_default_off_test")
    payload = module.sample_message_traces()

    assert payload["enabled"] is False
    assert payload["ok"] is True
    assert payload["error"] is None
    assert payload["traces"] == []


def test_public_observability_sources_have_no_private_ipv4_literals():
    for name in (
        "server.py",
        "transport_observability.py",
        "message_tracing.py",
        "app.js",
        "index.html",
        "styles.css",
        "andy-ops-panel.env.example",
    ):
        assert not PRIVATE_IPV4.search((PANEL_ROOT / name).read_text()), name


def test_frontend_renders_disabled_observability_sources():
    source = (PANEL_ROOT / "app.js").read_text()
    assert "obs.enabled === false" in source
    assert "tracePayload.enabled === false" in source


def test_runtime_observers_are_default_off(monkeypatch):
    monkeypatch.delenv("ANDY_OPS_TRANSPORT_OBSERVABILITY_ENABLED", raising=False)
    monkeypatch.delenv("ANDY_OPS_MESSAGE_TRACING_ENABLED", raising=False)
    monkeypatch.delenv("ANDY_OPS_TRACE_DB_CONTAINER", raising=False)

    transport = load(TRANSPORT, "andy_transport_observability_default_off_test")
    tracing = load(TRACING, "andy_message_tracing_default_off_test")

    transport_payload = transport.sample_transport_observability()
    trace_payload = tracing.sample_message_traces()

    assert transport_payload["enabled"] is False
    assert transport_payload["derived"]["severity"] == "DISABLED"
    assert trace_payload["enabled"] is False
    assert trace_payload["ok"] is True
    assert trace_payload["traces"] == []


def test_runtime_observer_flags_are_documented_as_disabled_by_default():
    env_example = (PANEL_ROOT / "andy-ops-panel.env.example").read_text()
    app = (PANEL_ROOT / "app.js").read_text()

    assert "ANDY_OPS_TRANSPORT_OBSERVABILITY_ENABLED=false" in env_example
    assert "ANDY_OPS_MESSAGE_TRACING_ENABLED=false" in env_example
    assert "obs.enabled === false" in app
    assert "tracePayload.enabled === false" in app
