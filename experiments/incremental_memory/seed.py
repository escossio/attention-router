"""Explicit one-time seed into the dedicated synthetic container."""

import json
from pathlib import Path
import subprocess

for item in json.loads(Path(__file__).with_name("fixtures.json").read_text()):
    subprocess.run(
        [
            "docker",
            "exec",
            "andy-memory-lab-core",
            "python",
            "/app/memory.py",
            "register",
            json.dumps(item),
        ],
        check=True,
    )
