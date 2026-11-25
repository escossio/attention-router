"""G3 must bind an exact durable maintenance hold to its approved operation.

All records are synthetic. No test communicates with GitHub or a live database.
"""
from datetime import UTC, datetime, timedelta
import json
import multiprocessing
import os
from pathlib import Path
import runpy
from uuid import uuid4

import pytest

from attention_router.config import settings
from attention_router.infrastructure.maintenance_gate import ENV, GateError, MaintenanceGate

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "ops/context-canary/release_schema.py"


def create_binding(gate):
    for participant in gate.status()["participants"]:
        gate.observe(participant)
    state = gate.request_drain()
    return {"gate_id": state["gate_id"], "window": state["window"],
            "hold": gate.claim_maintenance(state["window"]), "participants": state["participants"]}


def write_private(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    release = runpy.run_path(str(RUNNER))
    roster = sorted(release["SCHEMA_REQUIRED_PARTICIPANTS"])
    gate = MaintenanceGate.initialize(tmp_path / "gate", roster)
    binding = create_binding(gate)
    monkeypatch.setenv(ENV, str(gate.directory))
    monkeypatch.setenv("ANDY_SCHEMA_RELEASE_EXPLICIT", "SCHEMA_ONLY_NO_TENANT")
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    approval = {
        "version": 1, "operation": "schema_apply",
        "confirmation": release["CONFIRM"]["schema_apply"],
        "approved_at_utc": datetime.now(UTC).isoformat(),
        "maintenance_gate": binding,
        "source_sha256": release["source_digest"](ROOT),
    }
    approval["approval_ref"] = release["schema_approval_ref"](approval)
    record = {
        "version": 2, "gate": "G3", "operation": "schema_apply",
        "approval_ref": approval["approval_ref"],
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    }
    path = tmp_path / "synthetic-approval.json"
    write_private(path, approval)
    run = release["run"]
    calls = []

    def verify_github(*args):
        calls.append("github")
        return record

    def unexpected_engine(*args, **kwargs):
        pytest.fail("refused schema operation attempted to open database")

    monkeypatch.setitem(run.__globals__, "verify_github_release", verify_github)
    monkeypatch.setitem(run.__globals__, "create_engine", unexpected_engine)

    def invoke(action="apply"):
        return run(action, root=ROOT, acl_path=tmp_path / "absent-acl",
                   catalog_path=tmp_path / "absent-catalog", approval_path=path, github_comment_id=17)

    return release, gate, binding, approval, record, path, calls, invoke


@pytest.mark.parametrize("case,reason", [
    ("missing_gate", "RELEASE_MAINTENANCE_GATE_REQUIRED"),
    ("missing_binding", "RELEASE_MAINTENANCE_BINDING_REQUIRED"),
    ("old_opaque_ref", "RELEASE_MAINTENANCE_APPROVAL_BINDING_MISMATCH"),
    ("tampered_approval", "RELEASE_MAINTENANCE_APPROVAL_BINDING_MISMATCH"),
    ("short_roster", "RELEASE_MAINTENANCE_ROSTER_INVALID"),
    ("extra_roster", "RELEASE_MAINTENANCE_ROSTER_INVALID"),
    ("wrong_gate", "GATE_BINDING_MISMATCH"),
    ("wrong_window", "GATE_BINDING_MISMATCH"),
    ("wrong_hold", "GATE_BINDING_MISMATCH"),
    ("released_hold", "GATE_BINDING_MISMATCH"),
    ("expired", "RELEASE_APPROVAL_EXPIRED"),
    ("duplicate_field", "RELEASE_DUPLICATE_APPROVAL_FIELD"),
    ("previous_attempt", "GATE_EXECUTION_ALREADY_ATTEMPTED"),
])
def test_schema_apply_refusals_precede_database(fixture, monkeypatch, case, reason):
    release, gate, binding, approval, record, path, calls, invoke = fixture
    if case == "missing_gate":
        monkeypatch.delenv(ENV)
    elif case == "missing_binding":
        approval.pop("maintenance_gate")
    elif case == "old_opaque_ref":
        approval["approval_ref"] = "old-opaque-ref"
        record["approval_ref"] = "old-opaque-ref"
    elif case == "tampered_approval":
        approval["source_sha256"] = "changed"
    elif case == "short_roster":
        binding["participants"] = ["worker"]
    elif case == "extra_roster":
        binding["participants"] = sorted((*binding["participants"], "unexpected"))
    elif case in {"wrong_gate", "wrong_window", "wrong_hold"}:
        binding[{"wrong_gate": "gate_id", "wrong_window": "window", "wrong_hold": "hold"}[case]] = uuid4().hex
    elif case == "released_hold":
        gate.release_maintenance(binding["window"], binding["hold"])
    elif case == "expired":
        approval["approved_at_utc"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    elif case == "previous_attempt":
        with gate.execute_maintenance(binding, operation="schema_apply",
                                      approval_ref=approval["approval_ref"], comment_id=17):
            pass

    if case not in {"old_opaque_ref", "tampered_approval"}:
        approval["approval_ref"] = release["schema_approval_ref"](approval)
        record["approval_ref"] = approval["approval_ref"]
    write_private(path, approval)
    if case == "duplicate_field":
        path.write_text('{"version":1,' + json.dumps(approval)[1:], encoding="utf-8")
    with pytest.raises(RuntimeError, match=reason):
        invoke()
    assert calls == ["github"]


def test_schema_rollback_requires_own_binding(fixture, monkeypatch):
    release, gate, _, approval, record, path, calls, invoke = fixture
    approval.update(operation="schema_rollback", confirmation=release["CONFIRM"]["schema_rollback"])
    record["operation"] = "schema_rollback"
    approval["approval_ref"] = release["schema_approval_ref"](approval)
    record["approval_ref"] = approval["approval_ref"]
    write_private(path, approval)
    monkeypatch.delenv(ENV)
    with pytest.raises(RuntimeError, match="RELEASE_MAINTENANCE_GATE_REQUIRED"):
        invoke("rollback")
    assert calls == ["github"] and gate.status()["execution_state"] is None


def _die_in_execution(directory, binding):
    gate = MaintenanceGate(directory)
    with gate.execute_maintenance(binding, operation="schema_apply", approval_ref="a" * 64,
                                  comment_id=17):
        os._exit(23)


def test_process_death_retains_hold_and_consumed_attempt(tmp_path):
    gate = MaintenanceGate.initialize(tmp_path / "gate", ["worker"])
    binding = create_binding(gate)
    process = multiprocessing.get_context("spawn").Process(
        target=_die_in_execution, args=(str(gate.directory), binding))
    process.start()
    process.join(8)
    if process.is_alive():
        process.terminate()
        process.join()
        pytest.fail("synthetic child did not finish")
    assert process.exitcode == 23
    reopened = MaintenanceGate(gate.directory)
    assert reopened.status()["execution_state"] == "STARTED"
    with pytest.raises(GateError, match="GATE_EXECUTION_ALREADY_ATTEMPTED"):
        with reopened.execute_maintenance(binding, operation="schema_apply", approval_ref="a" * 64,
                                          comment_id=17):
            pytest.fail("crash allowed automatic retry")
    with pytest.raises(GateError, match="GATE_RECONCILIATION_REQUIRED"):
        reopened.release_maintenance(binding["window"], binding["hold"])
    with pytest.raises(GateError, match="GATE_RESUME_BLOCKED"):
        reopened.resume(binding["window"])


def test_execution_owns_hold_until_end_and_requires_reconciliation(tmp_path):
    gate = MaintenanceGate.initialize(tmp_path / "gate", ["worker"])
    binding = create_binding(gate)
    with gate.execute_maintenance(binding, operation="schema_apply", approval_ref="a" * 64,
                                  comment_id=17) as recheck:
        recheck()
        assert gate.status()["execution_state"] == "STARTED"
        with pytest.raises(GateError, match="GATE_EXECUTION_BUSY"):
            gate.release_maintenance(binding["window"], binding["hold"], reconciled=True)
        with pytest.raises(GateError, match="GATE_EXECUTION_BUSY"):
            with gate.execute_maintenance(binding, operation="schema_apply",
                                          approval_ref="a" * 64, comment_id=17):
                pytest.fail("second runner acquired same hold")
        with gate.admission("worker") as admitted:
            assert not admitted
        recheck()
    assert gate.status()["execution_state"] == "COMPLETED"
    with pytest.raises(GateError, match="GATE_RECONCILIATION_REQUIRED"):
        gate.release_maintenance(binding["window"], binding["hold"])
    gate.release_maintenance(binding["window"], binding["hold"], reconciled=True)
    gate.resume(binding["window"])
    with gate.admission("worker") as admitted:
        assert admitted


def test_execution_failure_preserves_unconfirmed_attempt(tmp_path):
    gate = MaintenanceGate.initialize(tmp_path / "gate", ["worker"])
    binding = create_binding(gate)
    with pytest.raises(RuntimeError, match="synthetic transaction error"):
        with gate.execute_maintenance(binding, operation="schema_rollback",
                                      approval_ref="b" * 64, comment_id=18):
            raise RuntimeError("synthetic transaction error")
    assert gate.status()["execution_state"] == "UNCONFIRMED"
    with pytest.raises(GateError, match="GATE_RECONCILIATION_REQUIRED"):
        gate.release_maintenance(binding["window"], binding["hold"])


def test_execution_lock_replacement_fails_closed(tmp_path):
    gate = MaintenanceGate.initialize(tmp_path / "gate", ["worker"])
    binding = create_binding(gate)
    lock = gate.directory / "execution.lock"
    lock.rename(gate.directory / "old-execution.lock")
    lock.touch(mode=0o600)
    with pytest.raises(GateError, match="GATE_EXECUTION_LOCK_REPLACED"):
        with gate.execute_maintenance(binding, operation="schema_apply",
                                      approval_ref="a" * 64, comment_id=17):
            pytest.fail("replacement inode authorized operation")
    with pytest.raises(GateError, match="GATE_EXECUTION_LOCK_REPLACED"):
        gate.release_maintenance(binding["window"], binding["hold"])


def test_source_digest_contains_maintenance_gate():
    release = runpy.run_path(str(RUNNER))
    assert "attention_router/infrastructure/maintenance_gate.py" in release["FILES"]
