#!/usr/bin/env python3
"""Reconcile the minimal live-runtime invariants required after an AGT reboot."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


class RuntimeInvariantError(RuntimeError):
    pass


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeInvariantError(f"{name} is required")
    return value


def compose_files() -> list[str]:
    values = [item for item in required("ATTENTION_LIVE_WORKER_COMPOSE_FILES").split(":") if item]
    if not values:
        raise RuntimeInvariantError("no worker compose files configured")
    return values


def run(args: list[str], *, cwd: str | None = None) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
        timeout=90,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[:400]
        raise RuntimeInvariantError(f"command failed ({result.returncode}): {detail}")
    return result.stdout.strip()


def inspect_value(container: str, template: str) -> str:
    return run(["docker", "inspect", "-f", template, container]).strip()


def container_env(container: str) -> dict[str, str]:
    raw = inspect_value(container, "{{range .Config.Env}}{{println .}}{{end}}")
    result: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            result[key] = value
    return result


def wait_healthy(container: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    last = "unknown"
    while time.monotonic() < deadline:
        status = inspect_value(container, "{{.State.Status}}")
        health = inspect_value(
            container,
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
        )
        last = f"{status}/{health}"
        if status == "running" and health in {"healthy", "none"}:
            return
        time.sleep(1)
    raise RuntimeInvariantError(f"{container} did not become healthy: {last}")


def worker_compose_command() -> list[str]:
    command = ["docker", "compose", "-p", required("ATTENTION_LIVE_PROJECT")]
    for item in compose_files():
        if not Path(item).is_file():
            raise RuntimeInvariantError(f"compose file missing: {item}")
        command.extend(["-f", item])
    return command


def verify() -> dict[str, str]:
    db = required("ATTENTION_LIVE_DB_CONTAINER")
    worker = required("ATTENTION_LIVE_WORKER_CONTAINER")
    db_restart = inspect_value(db, "{{.HostConfig.RestartPolicy.Name}}")
    worker_restart = inspect_value(worker, "{{.HostConfig.RestartPolicy.Name}}")
    env = container_env(worker)
    execution = env.get("AGENT_EXECUTION_ENABLED")

    if db_restart != "unless-stopped":
        raise RuntimeInvariantError(f"db restart policy drifted: {db_restart}")
    if worker_restart != "unless-stopped":
        raise RuntimeInvariantError(f"worker restart policy drifted: {worker_restart}")
    if execution != "true":
        raise RuntimeInvariantError(f"AGENT_EXECUTION_ENABLED drifted: {execution!r}")

    return {
        "db_restart": db_restart,
        "worker_restart": worker_restart,
        "agent_execution_enabled": execution,
        "db_health": inspect_value(db, "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"),
        "worker_health": inspect_value(worker, "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"),
    }


def reconcile() -> dict[str, str]:
    db = required("ATTENTION_LIVE_DB_CONTAINER")
    worker = required("ATTENTION_LIVE_WORKER_CONTAINER")
    workdir = required("ATTENTION_LIVE_WORKDIR")
    service = os.environ.get("ATTENTION_LIVE_WORKER_SERVICE", "worker").strip() or "worker"
    timeout_seconds = int(os.environ.get("ATTENTION_LIVE_HEALTH_TIMEOUT_SECONDS", "60"))

    run(["docker", "update", "--restart", "unless-stopped", db])
    run(["docker", "start", db])
    wait_healthy(db, timeout_seconds)

    command = worker_compose_command()
    command.extend(["up", "-d", "--no-deps", service])
    run(command, cwd=workdir)
    wait_healthy(worker, timeout_seconds)
    return verify()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    summary = verify() if args.check else reconcile()
    print(json.dumps({"status": "ok", **summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
