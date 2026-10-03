from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "ops/provisioning/andy-ops-panel"
TRACE = PANEL / "message_tracing.py"
TRANSPORT = PANEL / "transport_observability.py"
INSTALL = PANEL / "install.sh"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_message_tracing_requires_explicit_container(monkeypatch):
    monkeypatch.delenv("ANDY_OPS_TRACE_DB_CONTAINER", raising=False)
    module = load_module(TRACE, "andy_ops_message_trace_no_db")

    assert module.DB_CONTAINER == ""
    with pytest.raises(RuntimeError, match="TRACE_DB_CONTAINER_NOT_CONFIGURED"):
        module._psql_json("select '[]'::json")


def test_message_trace_builds_stage_metadata_without_message_payload():
    module = load_module(TRACE, "andy_ops_message_trace_stage")
    row = {
        "id": "evt_1",
        "correlation_id": "corr_1234567890",
        "source": "whatsapp",
        "event_type": "message.received",
        "status": "PROCESSED",
        "received_at": "2026-10-02T20:00:00+00:00",
        "processed_at": "2026-10-02T20:00:01+00:00",
        "interaction_id": "int_1",
        "audits": [],
        "queues": [],
        "decisions": [],
        "intents": [],
        "outbox": [],
        "message_body": "must never be projected",
    }

    trace = module._build_trace(row)

    assert trace["correlation_short"] == "corr_1234567"
    assert [stage["key"] for stage in trace["stages"]] == [
        "inbound",
        "ingress",
        "control",
        "queue",
        "decision",
        "execution",
        "outbox",
        "outbound",
    ]
    assert "message_body" not in trace
    assert "must never be projected" not in repr(trace)


def test_transport_observability_redacts_sensitive_journal_text():
    module = load_module(TRANSPORT, "andy_ops_transport_redaction")
    value = module._safe_journal_message(
        "Bearer secret-token https://private.example/path 5511999999999@c.us"
    )

    assert "secret-token" not in value
    assert "https://private.example/path" not in value
    assert "5511999999999" not in value
    assert "[redacted-url]" in value


def test_live_observability_package_has_no_private_runtime_defaults():
    combined = "\n".join(
        path.read_text()
        for path in (
            TRACE,
            TRANSPORT,
            PANEL / "andy-ops-panel.env.example",
        )
    )

    assert "attention-router-live-flow-stage4-db-1" not in combined
    assert "192.168.88." not in combined
    assert "10.77." not in combined


def test_installer_versions_live_observability_modules():
    content = INSTALL.read_text()

    assert 'transport_observability.py" "$INSTALL_DIR/transport_observability.py' in content
    assert 'message_tracing.py" "$INSTALL_DIR/message_tracing.py' in content
    assert '"$INSTALL_DIR/transport_observability.py"' in content
    assert '"$INSTALL_DIR/message_tracing.py"' in content
