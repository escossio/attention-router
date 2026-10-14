#!/usr/bin/env python3
"""Nonpublishing OCI controller readiness check. Safe while native publishers run."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
import subprocess

from probe import CONFIG, INSTALLATION_CONFIG, load_env, make_jwt


def report(name: str, callback) -> bool:
    try:
        callback()
    except Exception as error:
        # Never print exception messages: they may embed private paths or API data.
        print(f"FAIL {name} ({type(error).__name__})", flush=True)
        return False
    print(f"PASS {name}", flush=True)
    return True


def require(condition: bool) -> None:
    if not condition:
        raise RuntimeError("preflight contract not met")


def check_identity() -> None:
    require(os.getuid() == 10001 and os.getgid() == 10001)
    require(Path("/home/ci").is_dir())
    require(os.environ.get("HOME") == "/home/ci")


def check_database(path: Path, expected: set[str], writable: bool) -> None:
    require(path.is_file())
    mode = "rw" if writable else "ro"
    with sqlite3.connect(f"file:{path}?mode={mode}", uri=True, timeout=5) as db:
        require(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        require(expected.issubset(tables))
        if writable:
            db.execute("BEGIN IMMEDIATE")
            db.execute("SELECT 1")
            db.rollback()


def check_folders() -> None:
    for name in ["/var/log/andy-ci", "/ci-profiles"]:
        path = Path(name)
        require(path.is_dir() and os.access(path, os.R_OK | os.W_OK | os.X_OK))
    require(Path("/usr/local/lib/andy-ci/worker-capacity.json").is_file())


def check_signing_key() -> None:
    cfg = load_env(CONFIG)
    install = load_env(INSTALLATION_CONFIG)
    require(bool(cfg.get("GITHUB_APP_CLIENT_ID")))
    require(bool(install.get("GITHUB_APP_INSTALLATION_ID")))
    key = Path(cfg["GITHUB_APP_PRIVATE_KEY_PATH"])
    require(key.is_relative_to("/etc/andy-github-app") and key.is_file())
    require(bool(make_jwt(cfg["GITHUB_APP_CLIENT_ID"], str(key))))


def check_github_auth() -> None:
    from distributed_postgres_check import installation_token

    require(bool(installation_token()))


def check_worker(host: str) -> None:
    require(host in {"ci01", "ci02", "ci03"})
    result = subprocess.run(
        ["ssh", "-F", "/home/ci/.ssh/config", "-o", "BatchMode=yes",
         "-o", "ConnectTimeout=7", host,
         "test -x /srv/andy-ci/bin/andy-ci-run"],
        check=False, capture_output=True, timeout=15,
    )
    require(result.returncode == 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-write", action="store_true",
                        help="Write-lock test; use only on isolated copies or after drain")
    parser.add_argument("--github-auth", action="store_true",
                        help="Request installation token, never publish checks")
    parser.add_argument("--worker", action="append", choices=["ci01", "ci02", "ci03"],
                        default=[], help="SSH reachability without running tests")
    args = parser.parse_args()
    outcomes = [
        report("uid", check_identity),
        report("state_sqlite", lambda: check_database(
            Path("/var/lib/andy-github-app/events.sqlite3"),
            {"deliveries", "distributed_postgres_jobs", "distributed_shadow_jobs"},
            args.sqlite_write)),
        report("registry_sqlite", lambda: check_database(
            Path("/var/lib/andy-ci/host-registry.sqlite3"), set(),
            args.sqlite_write)),
        report("directories", check_folders),
        report("github_key_local_signing", check_signing_key),
    ]
    if args.github_auth:
        outcomes.append(report("github_installation_auth", check_github_auth))
    for host in args.worker:
        outcomes.append(report(f"worker_{host}", lambda host=host: check_worker(host)))
    print("PREFLIGHT_PASS" if all(outcomes) else "PREFLIGHT_FAIL", flush=True)
    return 0 if all(outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
