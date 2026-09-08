from datetime import UTC, datetime, timedelta
import json
import logging
from types import SimpleNamespace

from attention_router.observability.readiness_diagnostics import emit_readiness_diagnostic


def _evaluation(blockers=("PRESENCE",)):
    now = datetime(2026, 8, 28, tzinfo=UTC)
    signals = tuple(SimpleNamespace(
        dependency_id=name, state="BLOCKED", reason_code="TEST_BLOCKED",
        observed_at=now, freshness_expires_at=now + timedelta(seconds=1),
        source="test.adapter", metadata={"secret_test_marker": "no-leak"},
    ) for name in blockers)
    domain = SimpleNamespace(state="BLOCKED", blocker_references=blockers, evaluated_at=now)
    return SimpleNamespace(domain=domain, evidence=signals)


def test_blocked_diagnostic_is_allowlisted_and_specific(caplog):
    with caplog.at_level(logging.WARNING, logger="attention_router.readiness"):
        emit_readiness_diagnostic(
            attempt_id="attempt-1", correlation_id="corr-1", scenario_id="SCN-PE-029",
            execution_level="L1", evaluation=_evaluation(), handoff_result="READINESS_NOT_READY",
        )
    payload = json.loads(caplog.records[0].message)
    assert payload["signal_count"] == 1
    assert payload["signals"][0]["reason_code"] == "TEST_BLOCKED"
    assert "secret_test_marker" not in caplog.records[0].message
    assert "READINESS_NOT_READY" in caplog.records[0].message

