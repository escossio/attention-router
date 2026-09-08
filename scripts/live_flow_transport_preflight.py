#!/usr/bin/env python3
"""Read-only preflight for the canonical local WhatsApp transport."""

from __future__ import annotations

import json
import subprocess
from urllib.parse import urlsplit
from urllib.request import urlopen

from attention_router.config import settings


RETIRED_HA_TARGET = ("192.0.2.7", 18181)


def _get(url: str) -> dict:
    with urlopen(url, timeout=settings.local_transport_outbound_timeout_seconds) as response:
        return json.load(response)


def _systemd_state() -> tuple[bool, int]:
    result = subprocess.run(
        ["systemctl", "show", "attention-whatsapp-transport.service",
         "-p", "ActiveState", "-p", "MainPID", "--value"],
        capture_output=True, text=True, check=False,
    )
    values = result.stdout.splitlines()
    if len(values) < 2:
        return False, 0
    try:
        pid = int(values[1])
    except ValueError:
        pid = 0
    return result.returncode == 0 and values[0] == "active" and pid > 0, pid


def _listener_present(host: str, port: int) -> bool:
    result = subprocess.run(
        ["ss", "-ltn", f"sport = :{port}"],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0 and f"{host}:{port}" in result.stdout


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
    systemd_active, pid = _systemd_state()
    listener = _listener_present(host, port)
    print("RETIRED_HA_TRANSPORT_TARGET_ACTIVE=NO")
    print(f"SYSTEMD_ACTIVE={'YES' if systemd_active else 'NO'}")
    print(f"TRANSPORT_PID={pid}")
    print(f"LISTENER_18103={'YES' if listener else 'NO'}")
    print(f"CURRENT_TRANSPORT_PROCESS={'RUNNING' if systemd_active else 'UNKNOWN'}")
    healthy = all((
        systemd_active,
        listener,
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
