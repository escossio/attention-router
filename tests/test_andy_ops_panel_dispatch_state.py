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
