#!/usr/bin/env python3
"""Read-only, privacy-filtered Tempo trace tree helper."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import defaultdict


SAFE_ATTRIBUTES = {
    "attention.outcome",
    "attention.final_outcome",
    "attention.response_source",
    "attention.response_generated",
    "attention.external_send",
    "attention.repetition.semantic_repeat",
    "attention.repetition.state_changed",
    "attention.repetition.suppressed",
    "attention.repetition.reason",
    "attention.message_family",
    "attention.response_objective",
}


def _value(attribute: dict) -> object:
    value = attribute.get("value", {})
    if isinstance(value, dict):
        return next(iter(value.values()), "")
    return value


def _attributes(span: dict) -> dict[str, object]:
    result: dict[str, object] = {}
    for item in span.get("attributes", []):
        key = item.get("key")
        if key in SAFE_ATTRIBUTES:
            result[key] = _value(item)
    return result


def parse_trace(payload: dict) -> list[dict]:
    spans: list[dict] = []
    for batch in payload.get("batches", []):
        for scope in batch.get("scopeSpans", []):
            spans.extend(scope.get("spans", []))
    for resource in payload.get("resourceSpans", []):
        for scope in resource.get("scopeSpans", []):
            spans.extend(scope.get("spans", []))
    return spans


def fetch(endpoint: str, trace_id: str) -> dict:
    url = f"{endpoint.rstrip('/')}/api/traces/{trace_id}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def _duration(span: dict) -> str:
    start = int(span.get("startTimeUnixNano", 0))
    end = int(span.get("endTimeUnixNano", 0))
    return f"{max(0, end - start) / 1_000_000:.2f}ms"


def render(trace_id: str, spans: list[dict]) -> str:
    if not spans:
        raise ValueError("Tempo returned no spans")
    by_parent: dict[str, list[dict]] = defaultdict(list)
    roots: list[dict] = []
    for span in spans:
        parent = span.get("parentSpanId")
        if parent:
            by_parent[parent].append(span)
        else:
            roots.append(span)

    lines = [f"TRACE={trace_id}"]

    def visit(span: dict, prefix: str = "", last: bool = True) -> None:
        marker = "└── " if last else "├── "
        status = span.get("status", {}).get("message", "OK") or "OK"
        status_code = span.get("status", {}).get("code")
        if status_code == 2:
            status = "ERROR"
        attrs = _attributes(span)
        suffix = ", ".join(f"{key}={value}" for key, value in attrs.items())
        suffix = f"; {suffix}" if suffix else ""
        lines.append(f"{prefix}{marker}{span.get('name', '<unnamed>')} [{status}; {_duration(span)}{suffix}]")
        children = by_parent.get(span.get("spanId", ""), [])
        for index, child in enumerate(children):
            visit(child, prefix + ("    " if last else "│   "), index == len(children) - 1)

    for index, root in enumerate(roots):
        visit(root, "", index == len(roots) - 1)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace_id")
    parser.add_argument("--endpoint", default="http://192.0.2.6:3200")
    args = parser.parse_args()
    try:
        payload = fetch(args.endpoint, args.trace_id)
        print(render(args.trace_id, parse_trace(payload)))
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"Tempo query failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
