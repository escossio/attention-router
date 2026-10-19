"""Disposable package and direct systemd-style invocation tests."""

import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts.operations import build_andy_live_runtime_v3_package as package


@pytest.fixture
def staged(tmp_path):
    root = tmp_path / "lifecycle-v3"
    receipt = package.build(root)
    entrypoint = root / "scripts/operations/andy_live_runtime_v3_entrypoint.py"
    env = {**os.environ, "ATTENTION_LIVE_PACKAGE_ROOT": str(root),
           "ATTENTION_LIVE_V3_MODULE_SHA256": receipt["files"]["scripts/operations/andy_live_runtime_v3.py"],
           "ATTENTION_LIVE_V3_HELPER_SHA256": receipt["files"]["scripts/forensics/andy_worker_composition_snapshot.py"]}
    return root, entrypoint, env


def call(entrypoint, env, *args):
    return subprocess.run([str(entrypoint), *args], env=env, capture_output=True, text=True, check=False)


def test_direct_executable_preserves_argument_and_exit_code(staged):
    root, entrypoint, env = staged
    assert os.access(entrypoint, os.X_OK)
    assert (root / "package.sha256.json").stat().st_mode & 0o777 == 0o400
    result = call(entrypoint, env, "--check")
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"status": "blocked", "reason_code": "MANIFEST_PIN_MISSING"}
    assert not list(root.rglob("__pycache__"))
    invalid = call(entrypoint, env, "--invalid-option")
    assert invalid.returncode == 2


def test_missing_private_package_config_fails_closed(staged):
    _, entrypoint, env = staged
    env.pop("ATTENTION_LIVE_V3_MODULE_SHA256")
    result = call(entrypoint, env, "--check")
    assert result.returncode == 1
    assert json.loads(result.stdout)["reason_code"] == "PACKAGE_PIN_MISSING"


def test_module_drift_fails_before_import(staged):
    root, entrypoint, env = staged
    module = root / "scripts/operations/andy_live_runtime_v3.py"
    module.write_bytes(module.read_bytes() + b"\n# drift\n")
    result = call(entrypoint, env, "--check")
    assert result.returncode == 1
    assert json.loads(result.stdout)["reason_code"] == "PACKAGE_DRIFT"


def test_group_writable_package_fails_closed(staged):
    root, entrypoint, env = staged
    root.chmod(0o775)
    result = call(entrypoint, env, "--check")
    assert result.returncode == 1
    assert json.loads(result.stdout)["reason_code"] == "PACKAGE_PERMISSIONS_INVALID"


def test_missing_helper_and_symlink_fail_closed(staged):
    root, entrypoint, env = staged
    helper = root / "scripts/forensics/andy_worker_composition_snapshot.py"
    helper.unlink()
    result = call(entrypoint, env, "--check")
    assert result.returncode == 1
    assert json.loads(result.stdout)["reason_code"] == "PACKAGE_UNAVAILABLE"
    helper.symlink_to(Path("/dev/null"))
    result = call(entrypoint, env, "--check")
    assert result.returncode == 1
    assert json.loads(result.stdout)["reason_code"] == "PACKAGE_PERMISSIONS_INVALID"


def test_builder_refuses_overwrite(staged):
    root, _, _ = staged
    with pytest.raises(ValueError, match="PACKAGE_DESTINATION_INVALID"):
        package.build(root)


def test_builder_normalizes_intermediate_modes_with_group_writable_umask(tmp_path):
    old_umask = os.umask(0o002)
    try:
        root = tmp_path / "group-umask-package"
        package.build(root)
    finally:
        os.umask(old_umask)
    for relative in ("scripts", "scripts/operations", "scripts/forensics"):
        assert (root / relative).stat().st_mode & 0o777 == 0o755


def test_public_boot_example_has_six_ordered_inputs():
    example = (package.SOURCE_ROOT / "ops/andy-live-runtime-v3/boot.env.example").read_text()
    rows = [line for line in example.splitlines() if line.startswith("ATTENTION_LIVE_WORKER_COMPOSE_FILES=")]
    assert len(rows) == 1
    assert rows[0].partition("=")[2].split(":") == [
        "<base>", "<otel>", "<otel_e2e>", "<execution>", "<groups>", "<visitor_recovery>",
    ]
