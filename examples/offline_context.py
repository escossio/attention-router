"""Provider-free fixture using the shipped context/TTS boundary tests."""
from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    tests = ["tests/test_tts_adapter.py"]
    tests.extend(str(p.relative_to(root)) for p in sorted((root / "tests").glob("*history*.py")))
    tests.extend(str(p.relative_to(root)) for p in sorted((root / "tests").glob("*language*.py")))
    return subprocess.call([sys.executable, "-m", "pytest", "-q", *tests], cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
