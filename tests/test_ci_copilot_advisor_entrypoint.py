from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "module",
    (
        "scripts.ci_copilot_advisor",
        "scripts.ci_copilot_advisor_entrypoint",
    ),
)
def test_copilot_advisor_module_entrypoints_import_cleanly(module: str):
    result = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Copilot" in result.stdout
