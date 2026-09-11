"""Build a wheel and test it in an isolated environment with no Attention Router install."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv
from shutil import copyfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "sdks/python"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="andy-python-sdk-") as temporary:
        work = Path(temporary)
        subprocess.run([
            sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(work),
            str(PACKAGE),
        ], check=True, cwd=work)
        wheel, = work.glob("andy_integration_sdk-*.whl")
        environment = work / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run([str(python), "-I", "-m", "pip", "install", str(wheel)],
                       check=True, cwd=work)
        subprocess.run([
            str(python), "-I", "-m", "unittest", "discover", "-s", str(PACKAGE / "tests"), "-v",
        ], check=True, cwd=work)
        subprocess.run([str(python), "-I", str(PACKAGE / "examples/offline.py")],
                       check=True, cwd=work)
        # Test tooling is installed after proving the wheel works with runtime dependencies only.
        subprocess.run([str(python), "-I", "-m", "pip", "install", "mypy==2.3.1"],
                       check=True, cwd=work)
        copyfile(PACKAGE / "examples/offline.py", work / "offline.py")
        copyfile(PACKAGE / "tests/typing_consumer.py", work / "typing_consumer.py")
        subprocess.run([
            str(python), "-I", "-m", "mypy", "--strict", "offline.py", "typing_consumer.py",
        ], check=True, cwd=work)


if __name__ == "__main__":
    main()
