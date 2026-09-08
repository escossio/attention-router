#!/usr/bin/env python3
"""Read-only near-real-time watcher for one sanitized Attention Router trace."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from tempo_trace import parse_trace
except ImportError:  # pragma: no cover - supports module/test execution from repo root
    from scripts.tempo_trace import parse_trace


def _get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def _attribute(span: dict, key: str) -> object | None:
    for item in span.get("attributes", []):
        if item.get("key") != key:
            continue
        value = item.get("value", {})
        return next(iter(value.values()), None) if isinstance(value, dict) else value
    return None


def _trace_ids(search: dict) -> list[str]:
    values = search.get("traces") or search.get("traceIDs") or search.get("traceIds") or []
    result: list[str] = []
    for item in values:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            value = item.get("traceID") or item.get("traceId") or item.get("trace_id")
            if value:
                result.append(str(value))
    return result


def _fetch_trace(endpoint: str, trace_id: str) -> list[dict]:
    payload = _get(f"{endpoint.rstrip('/')}/api/traces/{trace_id}")
    return parse_trace(payload)


def _trace_service_names(payload: dict) -> set[str]:
    names: set[str] = set()
    for batch in payload.get("batches", []):
        for attribute in batch.get("resource", {}).get("attributes", []):
            if attribute.get("key") == "service.name":
                value = attribute.get("value", {})
                if isinstance(value, dict):
                    names.update(str(item) for item in value.values())
    for resource in payload.get("resourceSpans", []):
        for attribute in resource.get("resource", {}).get("attributes", []):
            if attribute.get("key") == "service.name":
                value = attribute.get("value", {})
                if isinstance(value, dict):
                    names.update(str(item) for item in value.values())
    return names


def _trace_start_unix_nano(payload: dict) -> int:
    spans = parse_trace(payload)
    return min((int(span.get("startTimeUnixNano", 0)) for span in spans), default=0)


def _live_db_binding_exists(inbound_event_id: str, target_binding: str, *, runner=subprocess.run) -> bool:
    query = (
        "select count(*) from inbound_events e "
        "join actor_bindings b on b.id='" + target_binding + "' "
        "and b.source=e.source and b.external_actor_id=e.payload->>'actor_id' "
        "and b.is_active is true where e.id='" + inbound_event_id + "';"
    )
    result = runner(
        ["docker", "exec", "attention-router-db-1", "psql", "-U", "attention_router",
         "-d", "attention_router", "-Atc", query],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "1"


def _live_db_lab_claim_exists(inbound_event_id: str, session_id: str, *, runner=subprocess.run) -> bool:
    query = "select count(*) from lab_conversation_inbounds where inbound_event_id='" + inbound_event_id + "' and session_id='" + session_id + "';"
    result = runner(
        ["docker", "exec", "attention-router-db-1", "psql", "-U", "attention_router", "-d", "attention_router", "-Atc", query],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "1"


def _matches_binding(spans: list[dict], binding_hash: str | None) -> bool:
    if not binding_hash:
        return True
    return any(_attribute(span, "attention.binding_id") == binding_hash for span in spans)


def _matches_inbound_event(spans: list[dict], inbound_event_id: str | None) -> bool:
    if not inbound_event_id:
        return True
    return any(_attribute(span, "attention.inbound_event_id") == inbound_event_id for span in spans)


def _render(trace_id: str, spans: list[dict]) -> None:
    names = {span.get("name") for span in spans}
    print(f"TRACE={trace_id[:8]}…")
    for name in (
        "inbound.receive", "actor.resolve", "policy.resolve", "decision.evaluate",
        "behavior.generate", "repetition_guard.evaluate", "memory.archive",
        "memory.extract", "autonomy.evaluate", "execution.intent", "outbox.enqueue",
        "transport.send",
    ):
        print(f"{name:<28} {'OK' if name in names else 'NOT_REACHED'}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://192.0.2.6:3200")
    parser.add_argument("--trace")
    parser.add_argument("--next", action="store_true")
    parser.add_argument("--binding")
    parser.add_argument("--inbound-event-id")
    parser.add_argument("--live-service", default="attention-router-worker")
    parser.add_argument("--target-binding")
    parser.add_argument("--since", type=float, default=time.time())
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--lab-session")
    parser.add_argument("--max-inbounds", type=int, default=1)
    args = parser.parse_args()
    if not args.trace and not args.next:
        parser.error("use --trace or --next")
    if args.binding and len(args.binding) == 36 and args.binding.count("-") == 4:
        args.binding = hashlib.sha256(args.binding.encode("utf-8")).hexdigest()[:16]
    armed_at = args.since
    print(f"WATCHER_ARMED_AT={armed_at:.6f}", flush=True)

    try:
        if args.trace:
            _render(args.trace, _fetch_trace(args.endpoint, args.trace))
            return 0

        deadline = time.time() + args.timeout
        accepted: set[str] = set()
        while time.time() < deadline and len(accepted) < args.max_inbounds:
            start = int(args.since)
            end = max(start + 1, int(time.time()))
            query = urllib.parse.urlencode({"start": start, "end": end, "limit": 50})
            try:
                search = _get(f"{args.endpoint.rstrip('/')}/api/search?{query}")
            except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
                print("TRACE_QUERY_RETRY", flush=True)
                time.sleep(2)
                continue
            for trace_id in _trace_ids(search):
                try:
                    payload = _get(f"{args.endpoint.rstrip('/')}/api/traces/{trace_id}")
                    spans = parse_trace(payload)
                except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
                    print("TRACE_FETCH_RETRY", flush=True)
                    continue
                event_ids = [
                    str(value) for span in spans
                    for value in [_attribute(span, "attention.inbound_event_id")]
                    if value
                ]
                service_match = args.live_service in _trace_service_names(payload)
                timestamp_match = _trace_start_unix_nano(payload) >= int(armed_at * 1_000_000_000)
                db_match = bool(
                    event_ids and args.target_binding and
                    _live_db_binding_exists(event_ids[0], args.target_binding)
                )
                lab_match = not args.lab_session or bool(event_ids and _live_db_lab_claim_exists(event_ids[0], args.lab_session))
                if (
                    service_match and timestamp_match and db_match and
                    lab_match and trace_id not in accepted and
                    _matches_binding(spans, args.binding) and
                    _matches_inbound_event(spans, args.inbound_event_id)
                ):
                    _render(trace_id, spans)
                    accepted.add(trace_id)
                    if len(accepted) >= args.max_inbounds:
                        return 0
            print("WAITING", flush=True)
            time.sleep(2)
        print("TIMEOUT")
        return 2
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"WATCH_ERROR={type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
