"""Same-origin, loopback-only reader for private contextual performance."""

import json
import os
import re
from urllib.error import URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

RUN = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
TRACE = re.compile(r"[0-9a-f]{32}")
MAX_BYTES = 2_000_000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def source():
    try:
        raw = urlsplit(os.getenv("ANDY_OPS_CONTEXT_PERFORMANCE_SOURCE", ""))
        if (raw.scheme == "http" and raw.hostname in {"127.0.0.1", "::1"} and raw.port
                and not (raw.username or raw.password or raw.query or raw.fragment)
                and raw.path in ("", "/")):
            return raw.geturl().rstrip("/")
    except ValueError:
        pass
    return None


def context_performance(route, raw_query):
    base = source()
    if base is None:
        return {"state": "NOT_CONFIGURED"}, 503
    params = {}
    try:
        if route == "view":
            values = parse_qs(raw_query, max_num_fields=3)
            if set(values) - {"run", "baseline", "trace"} or any(len(v) != 1 for v in values.values()):
                return {"state": "INVALID_SELECTION"}, 400
            run = values.get("run", [""])[0]
            baseline = values.get("baseline", ["historical"])[0]
            trace = values.get("trace", [""])[0]
            if (not RUN.fullmatch(run) or (baseline != "historical" and not RUN.fullmatch(baseline))
                    or (trace and not TRACE.fullmatch(trace))):
                return {"state": "INVALID_SELECTION"}, 400
            params = {"run": run, "baseline": baseline}
            if trace:
                params["trace"] = trace
        elif route != "runs" or raw_query:
            return {"state": "INVALID_SELECTION"}, 400
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(f"{base}/{route}" + ("?" + urlencode(params) if params else ""), timeout=2) as response:
            data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            return {"state": "PROJECTION_LIMIT"}, 502
        payload = json.loads(data)
        if not isinstance(payload, dict):
            return {"state": "INVALID_PROJECTION"}, 502
    except (TimeoutError, URLError, OSError, ValueError):
        return {"state": "UNAVAILABLE"}, 503
    payload.update(state="AVAILABLE")
    return payload, 200
