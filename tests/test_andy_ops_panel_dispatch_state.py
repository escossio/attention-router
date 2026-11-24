from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "ops/provisioning/andy-ops-panel/server.py"
SCHEDULER = (
    ROOT
    / "ops/provisioning/distributed-ci-lab/control-plane/andy-ci-distributed"
)


def load_panel():
    panel_dir = str(PANEL.parent)
    sys.path.insert(0, panel_dir)
    try:
        spec = importlib.util.spec_from_file_location("andy_ops_panel_test", PANEL)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(panel_dir)


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


@pytest.mark.parametrize(("reason", "code"), [
    ("NO_DISCOVERY_CAPACITY", 69),
    ("DISCOVERY_MANIFEST_INVALID", 70),
    ("REPROFILE_REQUIRED", 66),
])
def test_scheduler_persists_early_failure_summary(tmp_path, monkeypatch, reason, code):
    sha = "c" * 40
    digest = "d" * 64
    registry = tmp_path / "registry.py"
    registry.write_text(
        "import sys\n"
        "if sys.argv[3] == 'list-workers': print('ci03')\n"
        "if sys.argv[3] == 'field': print('synthetic-worker')\n",
    )
    capacity = tmp_path / "capacity.json"
    capacity.write_text('{"pytest_full_seconds":{"ci03":1}}')
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "parse-postgres-discovery.py").symlink_to(
        SCHEDULER.with_name("parse-postgres-discovery.py"),
    )
    ssh = tmp_path / "ssh"
    discovery = (
        f"CI_SHA={sha}\nCI_POSTGRES_DISCOVERY_SHA={sha}\n"
        "CI_SUITE=postgres\nCI_POSTGRES_FILE=tests/test_example.py\n"
        "CI_POSTGRES_FILE_COUNT=1\nCI_POSTGRES_TEST_COUNT=1\n"
        f"CI_POSTGRES_SELECTION_SHA256={digest}\nCI_STATUS=PASS\n"
    )
    if reason == "DISCOVERY_MANIFEST_INVALID":
        discovery = discovery.replace("CI_POSTGRES_FILE_COUNT=1", "CI_POSTGRES_FILE_COUNT=2")
    ssh.write_text(
        '#!/bin/bash\nif [[ "${*: -1}" == ANDY_CI_DISCOVER_POSTGRES=* ]]; then\n'
        "cat <<'DISCOVERY'\n" + discovery + "DISCOVERY\nfi\n",
    )
    ssh.chmod(0o700)
    for name, value in {
        "ANDY_CI_BASE": tmp_path,
        "ANDY_CI_LOG_ROOT": tmp_path / "logs",
        "ANDY_CI_PROFILE_ROOT": tmp_path / "profiles",
        "ANDY_CI_HOST_REGISTRY_CLI": registry,
        "ANDY_CI_REPLAN_POSTGRES": SCHEDULER.with_name("replan-postgres.py"),
        "ANDY_CI_CAPACITY_FILE": capacity,
        "ANDY_CI_SSH_BIN": ssh,
        "ANDY_CI_CAPACITY_WAIT_SECONDS": "0" if reason == "NO_DISCOVERY_CAPACITY" else "5",
    }.items():
        monkeypatch.setenv(name, str(value))
    result = subprocess.run(
        ["bash", str(SCHEDULER), sha, "postgres"],
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert result.returncode == code, result.stdout + result.stderr
    summaries = [line.split("=", 1)[1] for line in result.stdout.splitlines()
                 if line.startswith("CI_DISTRIBUTED_SUMMARY=")]
    assert len(summaries) == 1
    summary = json.loads(Path(summaries[0]).read_text())
    assert summary["sha"] == sha and summary["suite"] == "postgres"
    assert summary["status"] == "FAIL" and summary["failure_class"] == reason
    assert summary["return_code"] == code and summary["total_passed_tests"] == 0
