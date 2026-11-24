"""Reconcile contextual spans with Tempo without replaying any workload."""

from argparse import ArgumentParser
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from time import time_ns
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


LOCAL_ONLY = {"context.executor_wait", "context.after_disconnect"}
TRACE = re.compile(r"[0-9a-f]{32}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def base_url(value):
    url = urlsplit(value)
    if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "::1"} or not url.port
            or url.path not in {"", "/"} or url.username or url.password or url.query or url.fragment):
        raise ValueError("expected the existing loopback Tempo query endpoint")
    return value.rstrip("/")


def lookup(endpoint, item):
    identifier, expected = item
    http = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with http.open(f"{endpoint}/api/traces/{identifier}", timeout=5) as response:
            payload = response.read(8_000_001)
            if len(payload) > 8_000_000:
                raise ValueError("trace response too large")
            trace = json.loads(payload)
        spans = Counter(span["name"] for batch in trace["batches"]
                        for scope in batch["scopeSpans"] for span in scope["spans"])
    except HTTPError as error:
        return {"trace": identifier, "http": error.code, "missing": dict(expected)}
    except (OSError, ValueError, KeyError, TypeError):
        return {"trace": identifier, "http": None, "missing": dict(expected)}
    return {"trace": identifier, "http": 200, "missing": dict(expected - spans)}


def audit(folder, endpoint):
    endpoint = base_url(endpoint)
    metadata = json.loads((folder / "metadata.json").read_text())
    if metadata["id"] != folder.name or metadata["mode"] != "instrumented":
        raise ValueError("select an instrumented run directory")
    raw = (folder / "events.log").read_bytes()
    if not raw.endswith(b"\n"):
        raise ValueError("event snapshot ends within a record")
    events = [json.loads(line.removeprefix(b"g7_event ")) for line in raw.splitlines()
              if line.startswith(b"g7_event ")]
    offered = {row["trace"] for row in events if row["event"] == "client_offer"}
    expected = defaultdict(Counter)
    for row in events:
        trace, stage = row.get("trace"), row.get("stage")
        if (TRACE.fullmatch(trace or "") and trace in offered and row["event"] == "end"
                and stage and stage not in LOCAL_ONLY):
            expected[trace][stage] += 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(lambda item: lookup(endpoint, item), expected.items()))
    return {
        "version": 1, "run": metadata["id"], "sha": metadata["sha"], "tree": metadata["tree"],
        "events_sha256": sha256(raw).hexdigest(), "checked_ns": time_ns(),
        "run_complete": any(row["event"] == "load_complete" for row in events),
        "expected_traces": len(expected), "received_traces": sum(row["http"] == 200 for row in result),
        "expected_spans": sum(sum(row.values()) for row in expected.values()),
        "missing_spans": sum(sum(row["missing"].values()) for row in result),
        "query_errors": sum(row["http"] not in (200, 404) for row in result),
        "traces": result,
    }


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--tempo-url", required=True)
    args = parser.parse_args()
    value = audit(args.run_dir, args.tempo_url)
    with NamedTemporaryFile(mode="w", dir=args.run_dir, prefix=".otlp-", delete=False) as f:
        json.dump(value, f, indent=2)
        f.write("\n")
        temp = f.name
    os.replace(temp, args.run_dir / "otlp-coverage.json")
    print(json.dumps({key: item for key, item in value.items() if key != "traces"}))


if __name__ == "__main__":
    main()
