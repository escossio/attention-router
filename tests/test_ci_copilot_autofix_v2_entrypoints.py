from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "scripts.ci_copilot_autofix_proposal",
        "scripts.ci_copilot_autofix_persist",
    ],
)
def test_v2_module_entrypoints_are_importable(module: str):
    result = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
