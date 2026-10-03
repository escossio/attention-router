#!/usr/bin/env python3
"""Read-only WhatsApp transport observability for Andy Ops."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import subprocess
from typing import Any
from urllib.request import Request, urlopen

try:
    import websocket
except ImportError:  # pragma: no cover - runtime capability is reported
    websocket = None


TRANSPORT_STATUS_URL = os.environ.get("ANDY_OPS_WHATSAPP_TRANSPORT_STATUS_URL", "").strip()
BROWSER_DEBUG_URL = os.environ.get(
    "ANDY_OPS_WHATSAPP_BROWSER_DEBUG_URL", "http://127.0.0.1:9223"
).rstrip("/")
API_READY_URL = os.environ.get(
    "ANDY_OPS_ATTENTION_API_READY_URL", "http://127.0.0.1:28110/health/ready"
).strip()
TRANSPORT_UNIT = os.environ.get(
    "ANDY_OPS_WHATSAPP_TRANSPORT_UNIT", "attention-whatsapp-transport.service"
)
BROWSER_UNIT = os.environ.get(
    "ANDY_OPS_WHATSAPP_BROWSER_UNIT", "attention-whatsapp-browser.service"
)
OBSERVER_UNIT = os.environ.get(
    "ANDY_OPS_WHATSAPP_OBSERVER_UNIT", "attention-whatsapp-observer.service"
)


def _http_json(url: str, timeout: float = 1.0) -> dict[str, Any] | list[Any]:
    if not url:
        raise RuntimeError("URL_NOT_CONFIGURED")
    request = Request(url, headers={"User-Agent": "andy-ops-readonly-probe/1"})
    with urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP_{response.status}")
        return json.loads(response.read().decode("utf-8"))


def _systemd_unit(unit: str) -> dict[str, Any]:
    properties = [
        "ActiveState",
        "SubState",
        "MainPID",
        "NRestarts",
        "ExecMainStartTimestamp",
    ]
    result = subprocess.run(
        ["systemctl", "show", unit, "--no-pager", *[f"-p{name}" for name in properties]],
        capture_output=True,
        text=True,
        timeout=1.0,
        check=False,
    )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key] = value
    return {
        "unit": unit,
        "active": values.get("ActiveState") == "active",
        "active_state": values.get("ActiveState") or "unknown",
        "sub_state": values.get("SubState") or "unknown",
        "pid": int(values.get("MainPID") or 0),
        "restarts": int(values.get("NRestarts") or 0),
        "started_at": values.get("ExecMainStartTimestamp") or None,
    }


def _journal_records(unit: str, limit: int = 100) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["journalctl", "-u", unit, "-n", str(limit), "--no-pager", "-o", "json"],
        capture_output=True,
        text=True,
        timeout=1.2,
        check=False,
    )
    records = []
    for line in result.stdout.splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _record_time(record: dict[str, Any]) -> str | None:
    try:
        micros = int(record.get("__REALTIME_TIMESTAMP") or 0)
    except (TypeError, ValueError):
        return None
    if not micros:
        return None
    return datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc).astimezone().isoformat()


def _age_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return max(
            0,
            int((datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()),
        )
    except ValueError:
        return None


def _browser_page_probe() -> dict[str, Any]:
    result = {
        "debug_reachable": False,
        "whatsapp_page_count": 0,
        "native_state": None,
        "auth_state": None,
        "has_synced": None,
        "sync_handler_present": None,
        "wwebjs_present": None,
        "probe_error": None,
    }
    try:
        targets = _http_json(f"{BROWSER_DEBUG_URL}/json/list", timeout=1.0)
        result["debug_reachable"] = True
        pages = [
            target for target in targets
            if target.get("type") == "page"
            and str(target.get("url") or "").startswith("https://web.whatsapp.com/")
        ]
        result["whatsapp_page_count"] = len(pages)
        if len(pages) != 1:
            result["probe_error"] = "PAGE_TOPOLOGY_INVALID"
            return result
        if websocket is None:
            result["probe_error"] = "WEBSOCKET_CLIENT_UNAVAILABLE"
            return result

        target = pages[0]
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            result["probe_error"] = "CDP_TARGET_WITHOUT_WEBSOCKET"
            return result
        connection = websocket.create_connection(
            ws_url,
            timeout=1.0,
            suppress_origin=True,
        )
        try:
            expression = """(() => {
              let nativeState = null;
              let authState = null;
              let hasSynced = null;
              try {
                const socket = window.require?.('WAWebSocketModel')?.Socket;
                nativeState = socket?.state || null;
                hasSynced = socket?.hasSynced ?? null;
              } catch {}
              try { authState = window.AuthStore?.AppState?.state || null; } catch {}
              return {
                nativeState,
                authState,
                hasSynced,
                syncHandlerPresent: typeof window.onAppStateHasSyncedEvent === 'function',
                wwebjsPresent: typeof window.WWebJS !== 'undefined',
              };
            })()"""
            connection.send(json.dumps({
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {"expression": expression, "returnByValue": True},
            }))
            while True:
                message = json.loads(connection.recv())
                if message.get("id") == 1:
                    value = (
                        message.get("result", {})
                        .get("result", {})
                        .get("value", {})
                    )
                    result["native_state"] = value.get("nativeState")
                    result["auth_state"] = value.get("authState")
                    result["has_synced"] = value.get("hasSynced")
                    result["sync_handler_present"] = value.get("syncHandlerPresent")
                    result["wwebjs_present"] = value.get("wwebjsPresent")
                    break
        finally:
            connection.close()
    except Exception as error:
        result["probe_error"] = type(error).__name__
    return result


def _observer_probe(records: list[dict[str, Any]]) -> dict[str, Any]:
    latest = None
    for record in reversed(records):
        message = str(record.get("MESSAGE") or "")
        if not message.startswith("{"):
            continue
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            continue
        if payload.get("event") != "observer_event":
            continue
        latest = {
            "observed_at": payload.get("ts") or _record_time(record),
            "collection_event": payload.get("collection_event"),
            "message_type": payload.get("type"),
            "from_me": payload.get("from_me"),
        }
        break
    if latest is None:
        latest = {
            "observed_at": None,
            "collection_event": None,
            "message_type": None,
            "from_me": None,
        }
    latest["age_seconds"] = _age_seconds(latest["observed_at"])
    latest["fresh"] = (
        latest["age_seconds"] is not None and latest["age_seconds"] <= 30
    )
    return latest


def _safe_journal_message(message: str) -> str:
    value = re.sub(r"\b\d{5,}(?=@(?:c\.us|lid)\b)", "[redacted]", message)
    value = re.sub(r"https?://\S+", "[redacted-url]", value)
    value = re.sub(r"(?i)Bearer\s+\S+", "Bearer [redacted]", value)
    return value[:240]


_TIMELINE_MARKERS = (
    "Started attention-whatsapp-transport",
    "Stopping attention-whatsapp-transport",
    "Stopped attention-whatsapp-transport",
    "whatsapp_page_selection",
    "existing_page_attach_",
    "loading_screen",
    "owner_authority_",
    "auth_failure",
    "initialize failed",
    "inbound_message_observed",
)


def _transport_timeline(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    timeline = []
    for record in records:
        message = str(record.get("MESSAGE") or "")
        if not any(marker in message for marker in _TIMELINE_MARKERS):
            continue
        timeline.append({
            "timestamp": _record_time(record),
            "message": _safe_journal_message(message),
        })
    return timeline[-28:]


def _derive_state(
    page: dict[str, Any],
    transport: dict[str, Any],
    observer: dict[str, Any],
    api_ready: bool,
) -> dict[str, Any]:
    page_state = page.get("native_state") or page.get("auth_state")
    client_state = transport.get("client_state")
    ready = transport.get("ready") is True
    owner_configured = transport.get("owner_identity_configured") is True

    divergences: list[dict[str, str]] = []
    if (
        page_state == "CONNECTED"
        and page.get("has_synced") is True
        and page.get("sync_handler_present") is True
        and not ready
    ):
        divergences.append({
            "code": "SYNC_EDGE_MISSED_READY_ABSENT",
            "severity": "CRITICAL",
            "detail": "Página já sincronizou e handler existe, mas transport não recebeu READY.",
        })
    if page_state == "CONNECTED" and client_state != "CONNECTED":
        divergences.append({
            "code": "PAGE_CONNECTED_TRANSPORT_STATE_DIVERGED",
            "severity": "CRITICAL",
            "detail": f"Página=CONNECTED; transport={client_state or 'UNKNOWN'}",
        })
    if page_state == "CONNECTED" and not ready:
        divergences.append({
            "code": "PAGE_CONNECTED_TRANSPORT_NOT_READY",
            "severity": "CRITICAL",
            "detail": "WhatsApp conectado fora do transport, mas /ready está falso.",
        })
    if page.get("whatsapp_page_count") not in {None, 1}:
        divergences.append({
            "code": "WHATSAPP_PAGE_TOPOLOGY_INVALID",
            "severity": "CRITICAL",
            "detail": f"page_count={page.get('whatsapp_page_count')}",
        })
    if not api_ready:
        divergences.append({
            "code": "ATTENTION_API_NOT_READY",
            "severity": "CRITICAL",
            "detail": "Backend Attention Router não está READY.",
        })

    if ready:
        recovery_gate = {
            "state": "NOT_NEEDED",
            "reason": "transport_ready",
        }
    elif client_state != "CONNECTED":
        recovery_gate = {
            "state": "BLOCKED",
            "reason": f"periodic recovery exige client_state=CONNECTED; atual={client_state or 'UNKNOWN'}",
        }
    elif not owner_configured:
        recovery_gate = {
            "state": "BLOCKED",
            "reason": "owner identity não configurada",
        }
    else:
        recovery_gate = {
            "state": "ELIGIBLE",
            "reason": "client_state=CONNECTED e owner configurado",
        }

    severity = "CRITICAL" if any(
        item["severity"] == "CRITICAL" for item in divergences
    ) else ("WARN" if divergences else "OK")

    return {
        "severity": severity,
        "page_state": page_state,
        "transport_client_state": client_state,
        "transport_ready": ready,
        "observer_fresh": observer.get("fresh") is True,
        "divergence": bool(divergences),
        "divergences": divergences,
        "recovery_gate": recovery_gate,
    }


def sample_transport_observability() -> dict[str, Any]:
    browser_unit = _systemd_unit(BROWSER_UNIT)
    transport_unit = _systemd_unit(TRANSPORT_UNIT)
    observer_unit = _systemd_unit(OBSERVER_UNIT)

    try:
        transport = _http_json(TRANSPORT_STATUS_URL, timeout=1.0)
        if not isinstance(transport, dict):
            raise RuntimeError("INVALID_TRANSPORT_STATUS")
        transport_error = None
    except Exception as error:
        transport = {}
        transport_error = type(error).__name__

    page = _browser_page_probe()

    try:
        api_payload = _http_json(API_READY_URL, timeout=1.0)
        api_ready = isinstance(api_payload, dict) and api_payload.get("status") == "ready"
        api_error = None
    except Exception as error:
        api_payload = {}
        api_ready = False
        api_error = type(error).__name__

    observer_records = _journal_records(OBSERVER_UNIT, limit=120)
    observer = _observer_probe(observer_records)
    transport_records = _journal_records(TRANSPORT_UNIT, limit=180)
    derived = _derive_state(page, transport, observer, api_ready)

    return {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "browser": {**browser_unit, **page},
        "transport": {
            **transport_unit,
            **transport,
            "probe_error": transport_error,
        },
        "observer": {**observer_unit, **observer},
        "api": {
            "ready": api_ready,
            "status": api_payload.get("status") if isinstance(api_payload, dict) else None,
            "probe_error": api_error,
        },
        "derived": derived,
        "timeline": _transport_timeline(transport_records),
    }