#!/usr/bin/env python3
"""Read-only preflight for the canonical local WhatsApp transport."""

from __future__ import annotations

import json
import os
import subprocess
from urllib.parse import urlsplit
from urllib.request import urlopen

from attention_router.config import settings


RETIRED_HA_TARGET = ("192.0.2.7", 18181)


def _get(url: str) -> dict:
    with urlopen(url, timeout=settings.local_transport_outbound_timeout_seconds) as response:
        return json.load(response)


def _container_state() -> tuple[bool, int]:
    name = os.environ.get("ANDY_WHATSAPP_TRANSPORT_CONTAINER", "andy-whatsapp-transport")
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{json .State}}", name],
        capture_output=True, text=True, check=False, timeout=3,
    )
    if result.returncode != 0:
        return False, 0
    try:
        state = json.loads(result.stdout)
        pid = int(state.get("Pid") or 0)
        return (state.get("Running") is True
                and state.get("Health", {}).get("Status") == "healthy"
                and pid > 0), pid
    except (ValueError, TypeError, AttributeError):
        return False, 0


def main() -> int:
    parsed = urlsplit(settings.local_transport_outbound_url)
    host = parsed.hostname or ""
    port = parsed.port
    print(f"CURRENT_TRANSPORT_HOST={host}")
    print(f"CURRENT_TRANSPORT_PORT={port}")
    print(f"CURRENT_TRANSPORT_HEALTH_ENDPOINT={parsed.scheme}://{host}:{port}/status")
    print(f"CURRENT_TRANSPORT_READY_ENDPOINT={parsed.scheme}://{host}:{port}/ready")

    if (host, port) == RETIRED_HA_TARGET:
        print("RETIRED_HA_TRANSPORT_TARGET_ACTIVE=YES")
        return 1
    if parsed.path != "/internal/send":
        print("CANONICAL_LOCAL_TRANSPORT_PATH=FAIL")
        return 1

    status = _get(f"{parsed.scheme}://{host}:{port}/status")
    ready = _get(f"{parsed.scheme}://{host}:{port}/ready")
    container_healthy, pid = _container_state()
    print("RETIRED_HA_TRANSPORT_TARGET_ACTIVE=NO")
    print(f"CONTAINER_HEALTHY={'YES' if container_healthy else 'NO'}")
    print(f"TRANSPORT_PID={pid}")
    print(f"CURRENT_TRANSPORT_PROCESS={'RUNNING' if container_healthy else 'UNKNOWN'}")
    healthy = all((
        container_healthy,
        ready.get("status") == "ready",
        status.get("service_state") == "ready",
        status.get("browser_debug_reachable") is True,
        status.get("authenticated_event_seen") is True,
        status.get("wwebjs_connected") is True,
        status.get("ready") is True,
        status.get("client_state") == "CONNECTED",
        status.get("qr_seen") is False,
    ))
    print(f"TRANSPORT_HEALTH={'PASS' if healthy else 'FAIL'}")
    print(f"TRANSPORT_READY={'YES' if healthy else 'NO'}")
    return 0 if healthy else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError as exc:
        print(f"TRANSPORT_PREFLIGHT_ERROR={type(exc).__name__}")
        raise SystemExit(1) from exc
