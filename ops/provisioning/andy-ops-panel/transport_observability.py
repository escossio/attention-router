#!/usr/bin/env python3
"""Read-only, container-native WhatsApp observability for Andy Ops."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import Any
from urllib.request import Request, urlopen

ENABLED = os.environ.get("ANDY_OPS_TRANSPORT_OBSERVABILITY_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
TRANSPORT_STATUS_URL = os.environ.get("ANDY_OPS_WHATSAPP_TRANSPORT_STATUS_URL", "").strip()
API_READY_URL = os.environ.get("ANDY_OPS_ATTENTION_API_READY_URL", "http://127.0.0.1:28110/health/ready").strip()
OBSERVER_STATUS_FILE = Path(os.environ.get("ANDY_OPS_WHATSAPP_OBSERVER_STATUS_FILE", "/var/lib/attention-router/whatsapp-observer/status.json"))
CONTAINERS = {role: f"andy-whatsapp-{role}" for role in ("browser", "transport", "observer")}
SAFE_TRANSPORT_FIELDS = frozenset({
    "ready", "client_state", "wwebjs_connected", "browser_debug_reachable",
    "authenticated_event_seen", "authenticated_identity_match", "owner_identity_configured",
    "owner_command_authority_ready", "owner_identity_verification",
    "owner_command_authority_reason", "qr_seen", "disconnect_count", "page_count",
    "inbound_seen_count", "inbound_spool_pending", "inbound_spool_sending",
    "inbound_spool_quarantine", "source_revision",
    "owner_authority_last_invalidation_reason", "owner_authority_last_invalidation_at",
})
SAFE_OBSERVER_FIELDS = frozenset({
    "service_state", "browser_connected", "whatsapp_page_count", "connected_page_count",
    "listener_attached", "app_state", "events_seen", "last_event_at", "last_attach_at",
    "last_error_at", "capture_body",
})
TIMELINE_EVENTS = frozenset({
    "whatsapp_page_selection", "existing_page_attach_started", "existing_page_attach_succeeded",
    "existing_page_attach_failed", "auth_failure", "owner_authority_invalidated",
    "inbound_message_observed", "transport_ready", "transport_disconnected",
})


def _http_json(url: str) -> dict[str, Any]:
    if not url:
        raise ValueError("URL_NOT_CONFIGURED")
    with urlopen(Request(url, headers={"User-Agent": "andy-ops-readonly-probe/1"}), timeout=1) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("INVALID_STATUS")
    return payload


def _project(payload: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    return {key: value[:160] if isinstance(value, str) else value
            for key, value in payload.items() if key in allowed and
            (value is None or isinstance(value, (str, bool, int, float)))}


def _docker_states() -> dict[str, dict[str, Any]]:
    states = {role: {"exists": False, "running": None, "health": None, "restart_count": None,
                     "started_at": None, "image": None, "revision": None} for role in CONTAINERS}
    try:
        result = subprocess.run(["docker", "inspect", *CONTAINERS.values()], capture_output=True,
                                text=True, timeout=2, check=False)
        payload = json.loads(result.stdout or "[]")
        for item in payload:
            role = next((role for role, name in CONTAINERS.items() if item.get("Name") == f"/{name}"), None)
            if role is None:
                continue
            state = item.get("State") or {}
            states[role] = {
                "exists": True, "running": state.get("Running"),
                "health": (state.get("Health") or {}).get("Status"),
                "restart_count": item.get("RestartCount"), "started_at": state.get("StartedAt"),
                "image": item.get("Image"),
                "revision": (item.get("Config") or {}).get("Labels", {}).get("org.opencontainers.image.revision"),
            }
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return states


def _observer_status() -> dict[str, Any]:
    try:
        stat = OBSERVER_STATUS_FILE.stat()
        payload = json.loads(OBSERVER_STATUS_FILE.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("INVALID_OBSERVER_STATUS")
        result = _project(payload, SAFE_OBSERVER_FIELDS)
        result["status_age_seconds"] = max(0, int(datetime.now(timezone.utc).timestamp() - stat.st_mtime))
        result["status_fresh"] = result["status_age_seconds"] <= 60
        event_at = result.get("last_event_at")
        try:
            result["message_activity_age_seconds"] = max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(event_at.replace("Z", "+00:00"))).total_seconds())) if event_at else None
        except (ValueError, AttributeError):
            result["message_activity_age_seconds"] = None
        result["observer_health"] = "HEALTHY" if result.get("service_state") == "READY" and result.get("browser_connected") is True and result["status_fresh"] else "DEGRADED"
        return result
    except (OSError, ValueError, TypeError) as error:
        return {"service_state": None, "whatsapp_page_count": None, "status_fresh": False,
                "observer_health": "UNKNOWN", "probe_error": type(error).__name__}


def _timeline() -> list[dict[str, Any]]:
    try:
        result = subprocess.run(["docker", "logs", "--timestamps", "--tail", "120", CONTAINERS["transport"]],
                                capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    timeline = []
    for line in result.stdout.splitlines() + result.stderr.splitlines():
        stamp, _, message = line.partition(" ")
        marker = message.split(" ", 1)[0].strip("{}\"':,")
        if marker in TIMELINE_EVENTS:
            timeline.append({"timestamp": stamp, "message": marker})
    return timeline[-28:]


def _derive_state(browser: dict[str, Any], transport: dict[str, Any], observer: dict[str, Any],
                  docker: dict[str, dict[str, Any]], api_ready: bool | None) -> dict[str, Any]:
    divergences: list[dict[str, str]] = []
    def add(code: str, severity: str = "CRITICAL", detail: str = "") -> None:
        divergences.append({"code": code, "severity": severity, "detail": detail})

    for role in CONTAINERS:
        state = docker[role]
        if state["exists"] and (state["running"] is False or state["health"] == "unhealthy"):
            add(f"{role.upper()}_CONTAINER_NOT_HEALTHY")
        elif not state["exists"] or state["running"] is None or state["health"] is None:
            add("OBSERVABILITY_GAP", "WARN", f"docker_{role}_unknown")
    transport_available = transport.get("probe_error") is None
    observer_available = observer.get("probe_error") is None
    tp = transport.get("page_count") if transport_available else None
    op = observer.get("whatsapp_page_count") if observer_available else None
    if isinstance(tp, bool) or not isinstance(tp, int):
        tp = None
    if isinstance(op, bool) or not isinstance(op, int):
        op = None
    if tp is None and op is None:
        add("OBSERVABILITY_GAP", "WARN", "page_count_sources_unavailable")
        page_count = None
        completeness = "NONE"
    else:
        page_count = tp if tp is not None else op
        completeness = "FULL" if tp is not None and op is not None else "PARTIAL"
        if completeness == "PARTIAL":
            add("OBSERVABILITY_GAP", "WARN", "page_count_source_partial")
        if tp is not None and op is not None and tp != op:
            add("PAGE_COUNT_SOURCE_DIVERGENCE", detail=f"transport={tp};observer={op}")
        for source, count in (("transport", tp), ("observer", op)):
            if count is not None and count != 1:
                add("WHATSAPP_PAGE_TOPOLOGY_INVALID", detail=f"{source}_page_count={count}")
    if transport_available:
        if transport.get("ready") is False:
            add("TRANSPORT_NOT_READY")
        if transport.get("client_state") not in (None, "CONNECTED"):
            add("TRANSPORT_NOT_CONNECTED")
        if transport.get("browser_debug_reachable") is False:
            add("TRANSPORT_BROWSER_DEBUG_UNREACHABLE")
        if transport.get("authenticated_identity_match") is False or transport.get("owner_identity_verification") not in (None, "MATCH"):
            add("AUTH_IDENTITY_MISMATCH")
        if transport.get("qr_seen") is True:
            add("QR_OBSERVED")
    else:
        add("OBSERVABILITY_GAP", "WARN", "transport_status_unavailable")
    if observer_available:
        if observer.get("service_state") not in (None, "READY") or observer.get("status_fresh") is False:
            add("OBSERVER_NOT_READY")
        if observer.get("browser_connected") is False:
            add("OBSERVER_BROWSER_DISCONNECTED")
        if op is not None and observer.get("connected_page_count") not in (None, op):
            add("OBSERVER_PAGE_COUNT_DIVERGED")
    else:
        add("OBSERVABILITY_GAP", "WARN", "observer_status_unavailable")
    if api_ready is False:
        add("ATTENTION_API_NOT_READY")
    elif api_ready is None:
        add("OBSERVABILITY_GAP", "WARN", "api_status_unavailable")
    # One code per gap is enough; details from source probe errors remain in the source objects.
    unique = {item["code"]: item for item in divergences}
    divergences = list(unique.values())
    severity = "CRITICAL" if any(item["severity"] == "CRITICAL" for item in divergences) else "WARN" if divergences else "OK"
    return {"severity": severity, "divergence": bool(divergences), "divergences": divergences,
            "page_state": observer.get("app_state") or transport.get("client_state"),
            "page_count": page_count, "transport_page_count": tp, "observer_page_count": op,
            "evidence_completeness": completeness, "transport_client_state": transport.get("client_state"),
            "transport_ready": transport.get("ready"), "observer_health": observer.get("observer_health"),
            "recovery_gate": {"state": "NOT_NEEDED" if transport.get("ready") is True else "UNKNOWN",
                              "reason": "transport_ready" if transport.get("ready") is True else "transport_not_ready_or_unknown"}}


def sample_transport_observability() -> dict[str, Any]:
    result = {"generated_at": datetime.now(timezone.utc).isoformat(), "enabled": ENABLED,
              "runtime": "CONTAINER", "legacy": {"state": "RETIRED / MASKED"}}
    if not ENABLED:
        return {**result, "browser": {}, "transport": {}, "observer": {}, "docker": {}, "api": {},
                "derived": {"severity": "DISABLED", "divergence": False, "divergences": []}, "timeline": []}
    docker = _docker_states()
    try:
        transport = _project(_http_json(TRANSPORT_STATUS_URL), SAFE_TRANSPORT_FIELDS)
        transport["probe_error"] = None
    except (OSError, ValueError, TimeoutError) as error:
        transport = {"probe_error": type(error).__name__}
    observer = _observer_status()
    try:
        api_status = _http_json(API_READY_URL).get("status")
        api_ready = api_status == "ready"
        api = {"ready": api_ready, "status": api_status, "probe_error": None}
    except (OSError, ValueError, TimeoutError) as error:
        api_ready = None
        api = {"ready": None, "status": None, "probe_error": type(error).__name__}
    tp = transport.get("page_count")
    op = observer.get("whatsapp_page_count")
    browser = {**docker["browser"], "runtime": "container", "active": docker["browser"]["running"],
               "healthy": docker["browser"]["running"] is True and docker["browser"]["health"] == "healthy",
               "debug_reachable": transport.get("browser_debug_reachable"),
               "whatsapp_page_count": tp if isinstance(tp, int) else op if isinstance(op, int) else None,
               "page_count": tp if isinstance(tp, int) else op if isinstance(op, int) else None,
               "native_state": observer.get("app_state") or transport.get("client_state")}
    transport = {**docker["transport"], **transport, "runtime": "container", "active": docker["transport"]["running"]}
    observer = {**docker["observer"], **observer, "runtime": "container", "active": docker["observer"]["running"]}
    derived = _derive_state(browser, transport, observer, docker, api_ready)
    return {**result, "browser": browser, "transport": transport, "observer": observer,
            "docker": docker, "api": api, "derived": derived, "timeline": _timeline()}
