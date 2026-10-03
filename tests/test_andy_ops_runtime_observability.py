from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PANEL_ROOT = ROOT / "ops/provisioning/andy-ops-panel"
TRANSPORT = PANEL_ROOT / "transport_observability.py"
TRACING = PANEL_ROOT / "message_tracing.py"
SERVER = PANEL_ROOT / "server.py"
INSTALL = PANEL_ROOT / "install.sh"


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
