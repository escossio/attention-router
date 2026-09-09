from subprocess import run
import sys


def test_public_architecture_demo_is_offline_and_deterministic():
    result = run(
        [sys.executable, "examples/public_architecture_demo.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "DETERMINISTIC OFFLINE DEMO" in result.stdout
    assert "DEMO_NETWORK_PROVIDER_CALLS=0" in result.stdout
    assert "DEMO_REAL_CREDENTIALS=0" in result.stdout
