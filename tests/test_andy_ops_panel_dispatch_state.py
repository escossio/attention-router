from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "ops/provisioning/andy-ops-panel/server.py"
SCHEDULER = (
    ROOT
    / "ops/provisioning/distributed-ci-lab/control-plane/andy-ci-distributed"
)


def load_panel():
    spec = importlib.util.spec_from_file_location("andy_ops_panel_test", PANEL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_run(root: Path, sha_short: str = "c11b0ef7a1f3") -> Path:
    path = root / f"20261002T202503-{sha_short}-postgres-distributed"
    path.mkdir()
    return path


def test_recent_run_without_summary_is_not_running_without_live_process(tmp_path):
    panel = load_panel()
    panel.LOG_ROOT = tmp_path
    make_run(tmp_path)

    recent = panel._recent_dispatches(active_sha_shorts=set())

    assert recent[0]["sha_short"] == "c11b0ef7a1f3"
    assert recent[0]["status"] == "INCOMPLETE"


def test_recent_run_without_summary_is_running_only_when_sha_is_live(tmp_path):
    panel = load_panel()
    panel.LOG_ROOT = tmp_path
    make_run(tmp_path)

    recent = panel._recent_dispatches(
        active_sha_shorts={"c11b0ef7a1f3"}
    )

    assert recent[0]["status"] == "RUNNING"


def test_early_failure_summary_is_terminal_in_panel(tmp_path):
    panel = load_panel()
    panel.LOG_ROOT = tmp_path
    run = make_run(tmp_path)
    sha = "c11b0ef7a1f394e896aa494c56c5abf1c48a137b"
    (run / "summary.json").write_text(
        json.dumps(
            {
                "schema_version": 4,
                "sha": sha,
                "suite": "postgres",
                "status": "FAIL",
                "failure_class": "NO_DISCOVERY_CAPACITY",
                "return_code": 69,
                "wall_seconds": 41,
                "workers": {},
                "total_passed_tests": 0,
            }
        )
    )

    recent = panel._recent_dispatches(
        active_sha_shorts={"c11b0ef7a1f3"}
    )

    assert recent[0]["status"] == "FAIL"
    assert recent[0]["failure_class"] == "NO_DISCOVERY_CAPACITY"
    assert recent[0]["wall_seconds"] == 41


def test_scheduler_persists_early_failure_summary():
    content = SCHEDULER.read_text()

    assert "trap finalize_on_exit EXIT" in content
    assert '"failure_class": reason' in content
    assert '"return_code": rc' in content
    assert "TERMINAL_REASON=NO_DISCOVERY_CAPACITY" in content
    assert "TERMINAL_REASON=REPROFILE_REQUIRED" in content

def test_recent_generic_suite_summary_is_authoritative(tmp_path):
    panel = load_panel()
    panel.LOG_ROOT = tmp_path
    run = tmp_path / "20261002T213858-5bc1511239f1-python-distributed"
    run.mkdir()
    (run / "summary.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sha": "5bc1511239f1797e0e27002b3abb0472211511cf",
                "suite": "python",
                "status": "FAIL",
                "wall_seconds": 134,
                "selected_worker": "ci03",
                "failure_class": "TEST_FAILURE",
                "attempts": [
                    {
                        "attempt": 1,
                        "worker": "ci03",
                        "classification": "TEST_FAILURE",
                        "rc": 1,
                        "duration_seconds": 130,
                    }
                ],
            }
        )
    )

    recent = panel._recent_dispatches(active_sha_shorts=set())

    assert recent[0]["sha_short"] == "5bc1511239f1"
    assert recent[0]["suite"] == "python"
    assert recent[0]["status"] == "FAIL"
    assert recent[0]["failure_class"] == "TEST_FAILURE"
    assert recent[0]["workers"]["ci03"]["status"] == "TEST_FAILURE"


def test_dispatch_prefers_latest_generic_terminal_run(tmp_path):
    panel = load_panel()
    panel.LOG_ROOT = tmp_path

    postgres = tmp_path / "20261002T210000-f4e527acf391-postgres-distributed"
    postgres.mkdir()
    (postgres / "summary.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "sha": "f4e527acf391d52356f5cd96db99727a0f2253d5",
                "suite": "postgres",
                "status": "PASS",
                "wall_seconds": 458,
                "workers": {},
            }
        )
    )

    generic = tmp_path / "20261002T213858-5bc1511239f1-python-distributed"
    generic.mkdir()
    (generic / "summary.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sha": "5bc1511239f1797e0e27002b3abb0472211511cf",
                "suite": "python",
                "status": "FAIL",
                "wall_seconds": 134,
                "failure_class": "TEST_FAILURE",
                "attempts": [],
            }
        )
    )

    # Make the generic run unambiguously newer than the PostgreSQL run.
    generic.touch()

    recent = panel._recent_dispatches(active_sha_shorts=set())
    dispatch = panel._dispatch([], recent)

    assert dispatch is not None
    assert dispatch["status"] == "FAIL"
    assert dispatch["suite"] == "python"
    assert dispatch["sha_short"] == "5bc1511239f1"

