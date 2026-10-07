import os
import socket
import subprocess
import tempfile
from pathlib import Path


GUARD = Path(__file__).resolve().parents[1] / "ops/whatsapp-container/browser-profile-guard.sh"
COMPOSE = GUARD.with_name("compose.yaml")


def run_guard(profile: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", 'source "$1"; acquire_profile_guard "$2"', "guard", str(GUARD), str(profile)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def singleton_links(profile: Path, host: str, pid: int, socket_target: str = "/tmp/com.google.Chrome.test/SingletonSocket") -> None:
    (profile / "SingletonLock").symlink_to(f"{host}-{pid}")
    (profile / "SingletonSocket").symlink_to(socket_target)
    (profile / "SingletonCookie").symlink_to("1234567890")


def test_clean_profile_and_stale_same_host_links(tmp_path: Path) -> None:
    assert "SINGLETON_STATE=CLEAN" in run_guard(tmp_path).stdout
    singleton_links(tmp_path, os.uname().nodename, 99999999)
    result = run_guard(tmp_path)
    assert result.returncode == 0
    assert "SINGLETON_STATE=STALE_REMOVED" in result.stdout
    assert not list(tmp_path.glob("Singleton*"))
    assert "SINGLETON_STATE=CLEAN" in run_guard(tmp_path).stdout


def test_changed_hostname_and_live_pid_fail_closed(tmp_path: Path) -> None:
    singleton_links(tmp_path, "previous-container", 99999999)
    result = run_guard(tmp_path)
    assert result.returncode != 0
    assert "SINGLETON_STATE=AMBIGUOUS_BLOCKED" in result.stderr
    assert len(list(tmp_path.glob("Singleton*"))) == 3
    (tmp_path / "SingletonLock").unlink()
    (tmp_path / "SingletonLock").symlink_to(f"{os.uname().nodename}-{os.getpid()}")
    result = run_guard(tmp_path)
    assert result.returncode != 0
    assert "SINGLETON_STATE=ACTIVE" in result.stderr
    assert len(list(tmp_path.glob("Singleton*"))) == 3


def test_second_writer_fails_before_singleton_inspection(tmp_path: Path) -> None:
    first = subprocess.Popen(
        [
            "bash", "-c",
            'source "$1"; acquire_profile_guard "$2"; echo HELD; sleep 10',
            "guard", str(GUARD), str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert first.stdout is not None
        assert first.stdout.readline().strip() == "SINGLETON_STATE=CLEAN"
        assert first.stdout.readline().strip() == "HELD"
        second = run_guard(tmp_path)
        assert second.returncode != 0
        assert "SINGLETON_STATE=ACTIVE reason=operational_lock_held" in second.stderr
    finally:
        first.terminate()
        first.wait(timeout=5)


def test_stale_socket_inode_is_removed_but_live_listener_blocks(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    probe = bin_dir / "socat"
    probe.write_text(
        "#!/usr/bin/env python3\n"
        "import socket, sys\n"
        "s = socket.socket(socket.AF_UNIX)\n"
        "try: s.connect(sys.argv[-1].split(':', 1)[1])\n"
        "except ConnectionRefusedError: print('Connection refused', file=sys.stderr); sys.exit(1)\n"
    )
    probe.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    with tempfile.TemporaryDirectory(prefix="com.google.Chrome.guard-", dir="/tmp") as directory:
        target = str(Path(directory) / "SingletonSocket")
        singleton_links(tmp_path, os.uname().nodename, 99999999, target)
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(target)
        listener.listen(1)
        try:
            result = run_guard(tmp_path, env)
            assert result.returncode != 0
            assert "SINGLETON_STATE=ACTIVE reason=socket_listener_present" in result.stderr
        finally:
            listener.close()
        result = run_guard(tmp_path, env)
        assert result.returncode == 0
        assert "SINGLETON_STATE=STALE_REMOVED" in result.stdout
        Path(target).unlink()


def test_compose_keeps_stable_hostname_and_disables_forwarding() -> None:
    compose = COMPOSE.read_text()
    browser = compose.split("  browser:\n", 1)[1].split("  transport:\n", 1)[0]
    transport = compose.split("  transport:\n", 1)[1].split("  observer:\n", 1)[0]
    assert "hostname: andy-whatsapp-browser" in browser
    assert 'net.ipv4.ip_forward: "0"' in browser
    assert 'net.ipv4.ip_forward: "0"' in transport
    assert "seccomp:unconfined" not in browser
    assert "privileged: true" not in compose
    assert "SYS_ADMIN" not in compose
