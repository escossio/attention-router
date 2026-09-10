from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_copilot_advisor_module_entrypoint_imports_cleanly():
    result = subprocess.run(
        [sys.executable, "-m", "scripts.ci_copilot_advisor", "--help"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Copilot" in result.stdout
