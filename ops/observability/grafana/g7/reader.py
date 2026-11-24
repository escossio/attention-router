"""Read-only projection of isolated M1 telemetry for Grafana Infinity."""

from argparse import ArgumentParser
from collections import Counter, defaultdict
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from math import ceil
from pathlib import Path
import re
from time import time_ns
from urllib.parse import parse_qs, urlsplit

RUN = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
TRACE = re.compile(r"[0-9a-f]{32}")
WAIT = {"context.executor_wait", "context.inference_wait"}
BUCKETS_MS = (1, 5, 20, 100, 250, 500, 1000, 2000, 4000, 8000, 10000, 15000, 30000)
STAGE = re.compile(r"context\.[a-z_]{1,32}")
FAILURES = {
    "application_timeout", "compute_deadline", "pool_exhausted", "database_lock_timeout",
    "sql_cancel_or_deadline", "anchor_lock", "dependency_unavailable", "inference_failure",
    "request_input_timeout", "internal_error",
}
AUTHORITY_REFUSALS = {"authority_refusal", "authority_changed"}


def percentile(values, p):
    return sorted(values)[ceil(len(values) * p) - 1] if values else None


def ms(nanos):
    return round(nanos / 1_000_000, 3)


def union_duration(intervals):
    """Union of intervals in one monotonic clock domain."""
    total = 0
    right = 0
    for low, high in sorted(intervals):
        total += max(0, high - max(low, right))
        right = max(high, right)
    return total


def trace_timeline(events):
    """Attribute exclusive time within observed, contained monotonic intervals."""
    intervals = {}
    for index, row in enumerate(events):
        duration = row.get("duration_ns")
        stamp = row.get("monotonic_ns")
        if (row["event"] == "end" and row.get("stage") != "context.after_disconnect"
                and type(duration) is int and type(stamp) is int):
            intervals[index] = (stamp - duration, stamp)
    output = []
    for index, row in enumerate(events):
        view = {key: row[key] for key in (
            "event", "stage", "timestamp_ns", "duration_ns", "outcome", "correlation",
            "sql_count", "used", "limit", "waiting",
        ) if key in row}
        if index in intervals:
            low, high = intervals[index]
            nested = [interval for child, interval in intervals.items() if child != index
                      and low <= interval[0] and interval[1] <= high and interval != (low, high)]
            view["exclusive_observed_ns"] = max(0, high - low - union_duration(nested))
        output.append(view)
    return output


@lru_cache(maxsize=6)
def read_events(path, _mtime, _size):
    events = []
    with path.open("rb") as stream:
        for line in stream:
            if not line.endswith(b"\n") or not line.startswith(b"g7_event "):
                continue
            try:
                row = json.loads(line.removeprefix(b"g7_event "))
            except ValueError:
                continue
            if isinstance(row, dict) and type(row.get("timestamp_ns")) is int:
                events.append(row)
    return events


def events_in(folder):
    path = folder / "events.log"
    if not path.is_file():
        return []
    stat = path.stat()
    return read_events(path, stat.st_mtime_ns, stat.st_size)


def major_latency(events, kind, outcomes=None):
    return [ms(row["duration_ns"]) for row in events if row["event"] == kind
            and type(row.get("duration_ns")) is int
            and (outcomes is None or row.get("outcome") in outcomes)]


def project_pool(events, selected_trace=""):
    phases = defaultdict(list)
    waiting, active = {}, {}
    disconnected = set()
    durations = defaultdict(list)
    selected_events = []
    metrics = Counter()
    capacity = latest_mono = None
    sampler_state = "unavailable"
    for row in events:
        event, trace = row["event"], row.get("trace")
        stage = row.get("stage")
        stamp = row.get("monotonic_ns")
        if type(stamp) is int:
            latest_mono = max(latest_mono or 0, stamp)
        if event == "pool_sampler":
            sampler_state = row["sampler_state"]
        if trace and event == "disconnect":
            disconnected.add(trace)
        if trace and event in {"begin", "end"} and STAGE.fullmatch(stage or ""):
            stack = phases[trace]
            if event == "begin":
                stack.append(stage)
            elif stage in stack:
                stack.pop(len(stack) - 1 - stack[::-1].index(stage))

        lease = row.get("pool_lease")
        if not isinstance(lease, str) or not TRACE.fullmatch(lease):
            continue
        if selected_trace and trace == selected_trace:
            selected_events.append(row)
        if type(row.get("pool_limit")) is int:
            capacity = row["pool_limit"]
        if event == "pool_request":
            waiting[lease] = row
        elif event == "pool_checkout":
            waiting.pop(lease, None)
            active[lease] = {"checkout": row, "last": row, "lock_samples": 0}
            durations["Acquisition"].append(ms(row["duration_ns"]))
        elif event == "pool_acquire_failed":
            waiting.pop(lease, None)
            if row.get("outcome") == "pool_exhausted":
                metrics["timeouts"] += 1
                durations["Acquisition timeout"].append(ms(row["duration_ns"]))
        elif event == "pool_checkin":
            active.pop(lease, None)
            for field, label in (("duration_ns", "Hold"), ("pool_sql_ns", "SQL cursor"),
                                 ("pool_outside_cursor_ns", "Outside cursor"),
                                 ("pool_after_disconnect_ns", "Hold after disconnect")):
                if field in row and (field != "pool_after_disconnect_ns" or row[field]):
                    durations[label].append(ms(row[field]))
            for field in ("pool_sql_count", "pool_begins", "pool_commits", "pool_rollbacks",
                          "pool_lock_timeouts"):
                metrics[field] += row.get(field, 0)
        elif event == "pool_progress" and lease in active:
            active[lease]["last"] = row
        elif event == "pool_pg_lock_sample":
            metrics["lock_samples"] += 1
            if lease in active:
                active[lease]["lock_samples"] += 1

    def open_row(row, state, start, latest, samples=None):
        trace = row.get("trace")
        return {"lease": row["pool_lease"], "trace": trace, "state": state,
                "age_ms": ms(max(0, latest_mono - start)) if latest_mono is not None and start else None,
                "phase": phases[trace][-1] if phases[trace] else None,
                "driver_active": latest.get("pool_sql_active"), "sql_count": latest.get("pool_sql_count"),
                "outside_cursor_ms": ms(latest["pool_outside_cursor_ns"]) if "pool_outside_cursor_ns" in latest else None,
                "lock_samples": samples, "disconnected": trace in disconnected}

    open_rows = [open_row(record["checkout"], "ocupada", record["checkout"].get("monotonic_ns"),
                          record["last"], record["lock_samples"]) for record in active.values()]
    open_rows.extend(open_row(row, "aguardando", row.get("monotonic_ns"), row) for row in waiting.values())
    occupied = len(active) if capacity is not None else None
    return {
        "pool_summary": [{"capacity": capacity, "occupied": occupied,
                          "free": max(0, capacity - occupied) if capacity is not None and occupied is not None else None,
                          "waiting": len(waiting) if capacity is not None else None, "sampler_state": sampler_state,
                          **{key: metrics[key] if capacity is not None else None for key in (
                              "timeouts", "lock_samples", "pool_lock_timeouts", "pool_sql_count",
                              "pool_begins", "pool_commits", "pool_rollbacks",
                          )}}],
        "pool_open": sorted(open_rows, key=lambda row: -(row["age_ms"] or 0)),
        "pool_distribution": [{"measure": label, "samples": len(values), "p50_ms": percentile(values, .5),
                               "p95_ms": percentile(values, .95), "p99_ms": percentile(values, .99),
                               "max_ms": max(values)} for label, values in sorted(durations.items())],
        "pool_trace": selected_events[-160:],
    }


def summarize(events, workload="load"):
    offers = {row["trace"] for row in events if row["event"] == "client_offer"
              and row["workload"] == workload}
    rows = [row for row in events if row.get("trace") in offers]
    stages = defaultdict(list)
    trace_events = defaultdict(list)
    active = Counter()
    clients = Counter()
    servers = Counter()
    empty_responses = set()
    authority_refusals = set()
    admitted = 0
    candidates = []
    for row in rows:
        trace = row["trace"]
        event = row["event"]
        stage = row.get("stage")
        if event == "client_result":
            clients[row["outcome"]] += 1
            if row["outcome"] == "empty_response":
                empty_responses.add(trace)
        elif event == "admitted":
            admitted += 1
        elif event == "candidates":
            candidates.append(row["count"])
        elif type(stage) is str and STAGE.fullmatch(stage):
            trace_events[trace].append(row)
            if event == "begin":
                active[stage] += 1
            elif event == "end" and type(row.get("duration_ns")) is int:
                active[stage] -= 1
                stages[stage].append(row)
                if stage == "context.request":
                    servers[row.get("outcome")] += 1
                    if row.get("outcome") in AUTHORITY_REFUSALS:
                        authority_refusals.add(trace)
    duration = major_latency(rows, "client_result")
    summary = {
        "offered": len(offers), "admitted": admitted, "completed": sum(clients.values()),
        "grounded": clients["grounded"], "abstained": clients["abstention"],
        "semantic_mismatch": clients["semantic_mismatch"],
        "client_timeout": clients["client_timeout"], "server_empty": clients["empty_response"],
        "transport_saturation": clients["transport_saturation"], "in_progress": max(0, active["context.request"]),
        "executor_waiting": max(0, active["context.executor_wait"]),
        "worker_active": max(0, active["context.worker"]),
        "disconnects": servers["client_disconnected"], "cancelled": servers["cancelled"],
        "application_failures": sum(servers[key] for key in FAILURES),
        "legitimate_refusals": len(authority_refusals),
        "candidates_p95": percentile(candidates, .95),
        "p50_ms": percentile(duration, .50), "p95_ms": percentile(duration, .95),
        "p99_ms": percentile(duration, .99),
    }
    eligible_completed = summary["completed"] - len(empty_responses & authority_refusals)
    exact = summary["grounded"] + summary["abstained"]
    successful = exact + summary["semantic_mismatch"]
    summary["availability_pct"] = round(100 * successful / eligible_completed, 2) if eligible_completed else None
    summary["grounding_pct"] = round(100 * exact / successful, 2) if successful else None
    minutes = (max(row["timestamp_ns"] for row in rows) -
               min(row["timestamp_ns"] for row in rows)) / 60_000_000_000 if rows else 0
    summary["throughput_per_min"] = round(summary["completed"] / minutes, 2) if minutes > 0 else None
    if rows:
        summary["latest_event_ns"] = max(row["timestamp_ns"] for row in rows)
        summary["latest_event_age_ms"] = ms(max(0, time_ns() - summary["latest_event_ns"]))
    components = []
    for name, samples in stages.items():
        times = [ms(row["duration_ns"]) for row in samples]
        components.append({"stage": name, "samples": len(times),
                           "kind": "wait" if name in WAIT else "inclusive",
                           "p50_ms": percentile(times, .5), "p95_ms": percentile(times, .95),
                           "p99_ms": percentile(times, .99),
                           "driver_ms": ms(sum(r.get("sql_ns", 0) for r in samples)),
                           "sql_count": sum(r.get("sql_count", 0) for r in samples)})
    intervals = []
    for identifier, value in trace_events.items():
        root = [r for r in value if r["event"] == "end" and r["stage"] == "context.request"]
        if not root:
            continue
        ending = root[-1]
        wait = []
        for row in value:
            if row["event"] == "end" and row["stage"] in WAIT:
                end = row["monotonic_ns"]
                wait.append((end - row["duration_ns"], end))
        intervals.append({"trace": identifier, "result": ending.get("outcome"),
                          "server_ms": ms(ending["duration_ns"]), "observed_wait_ms": ms(union_duration(wait)),
                          "unknown_or_processing_ms": ms(max(0, ending["duration_ns"] - union_duration(wait)))})
    distribution = []
    for upper in BUCKETS_MS:
        distribution.append({"upper_ms": upper, "cumulative": sum(value <= upper for value in duration)})
    summary["offers_over_histogram"] = sum(t > BUCKETS_MS[-1] for t in duration)
    # One chronological row per observed second. Retain count and aggregate only.
    evolution = defaultdict(Counter)
    for row in rows:
        point = row["timestamp_ns"] // 1_000_000_000 * 1000
        if row["event"] in ("client_offer", "client_result", "admitted"):
            evolution[point][row["event"]] += 1
    return {
        "summary": [summary], "components": sorted(components, key=lambda row: -row["p95_ms"]),
        "traces": sorted(intervals, key=lambda row: -row["server_ms"]),
        "distribution": distribution,
        "evolution": [{"time": time, "offered": counter["client_offer"], "admitted": counter["admitted"],
                       "completed": counter["client_result"]} for time, counter in sorted(evolution.items())],
        "outcomes": [{"outcome": name, "count": n} for name, n in sorted(servers.items())],
        "capacity": [{"time": row["timestamp_ns"] // 1_000_000, "resource": row["event"],
                      "used": row["used"], "limit": row["limit"], "waiting": row["waiting"]}
                     for row in rows if row["event"] in ("pool_capacity", "executor_capacity") and "used" in row],
        "resources": [{"time": row["timestamp_ns"] // 1_000_000, **{
            k: row[k] for k in ("cpu_seconds", "rss_peak_kib", "input_blocks", "output_blocks")
        }} for row in events if row["event"] == "resources"],
    }


def compare(current, baseline, meta, prior):
    compatible_fields = ("worker", "db_image", "corpus_digest", "queries_digest", "config", "condition")
    differences = [key for key in compatible_fields if meta.get(key) != prior.get(key)]
    comparable = bool(prior.get("config")) and not differences
    fields = ("p95_ms", "p50_ms", "p99_ms", "throughput_per_min", "grounded", "abstained",
              "client_timeout", "server_empty", "availability_pct", "grounding_pct")
    rows = []
    for field in fields:
        value, base = current.get(field), baseline.get(field)
        if value is None or base is None:
            continue
        rows.append({"metric": field, "selected": value, "baseline": base,
                     "delta": round(value - base, 3),
                     "delta_pct": round(100 * (value - base) / base, 2) if base else None})
    return rows, [{"comparable": comparable, "differences": ", ".join(differences) or
                   ("none" if comparable else "historical run has partial manifest")}]


class Projection:
    def __init__(self, root, baseline):
        self.root = root.resolve()
        self.baseline = baseline

    def metadata(self, run):
        if not RUN.fullmatch(run):
            raise ValueError("invalid run")
        folder = self.root / run
        if folder.is_symlink():
            raise ValueError("invalid run")
        return json.loads((folder / "metadata.json").read_text()), folder

    def get(self, path, query):
        if path == "/runs":
            runs = []
            for p in self.root.iterdir():
                if RUN.fullmatch(p.name) and (p / "metadata.json").is_file() and not p.is_symlink():
                    run, _ = self.metadata(p.name)
                    runs.append({"id": p.name, "label": f"{p.name} · {run['sha'][:12]} · {run['mode']}",
                                 "started_ns": run["started_ns"]})
            return {"runs": sorted(runs, key=lambda row: row["started_ns"], reverse=True),
                    "baselines": [{"id": "historical", "label": "M1 histórico · 199 segmentos"}, *runs]}
        if path != "/view":
            raise ValueError("unknown route")
        run = query.get("run", [""])[0]
        metadata, folder = self.metadata(run)
        events = events_in(folder)
        result = summarize(events)
        result["fault_traces"] = summarize(events, "fault")["traces"]
        result["run_state"] = "complete" if any(row["event"] == "load_complete" for row in events) else "running"
        result["metadata"] = [{"field": key, "value": str(value)} for key, value in
                              (metadata | metadata["config"]).items() if key != "config"]
        baseline = query.get("baseline", ["historical"])[0]
        if baseline == "historical":
            base = self.baseline
            before = {"summary": [base["summary"]], "components": [], "distribution": [], "resources": []}
        else:
            base, base_folder = self.metadata(baseline)
            before = summarize(events_in(base_folder))
        result["comparison"], result["comparability"] = compare(
            result["summary"][0], before["summary"][0], metadata, base,
        )
        result["baseline_distribution"] = before["distribution"]
        old_stages = {row["stage"]: row for row in before["components"]}
        result["component_comparison"] = [
            {"stage": row["stage"], "kind": row["kind"], "selected_ms": row["p95_ms"],
             "baseline_ms": old_stages[row["stage"]]["p95_ms"],
             "delta_ms": round(row["p95_ms"] - old_stages[row["stage"]]["p95_ms"], 3)
             } for row in result["components"] if row["stage"] in old_stages
        ]
        result["baseline_resources"] = before["resources"]
        trace = query.get("trace", [""])[0]
        result.update(project_pool(events, trace if TRACE.fullmatch(trace) else ""))
        result["timeline"] = trace_timeline([
            row for row in events if TRACE.fullmatch(trace) and row.get("trace") == trace
        ])
        result["read_ns"] = time_ns()
        result["summary"][0]["read_ns"] = result["read_ns"]
        return result


def serve(port, projection):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            try:
                url = urlsplit(self.path)
                data = projection.get(url.path, parse_qs(url.query, max_num_fields=8))
                raw = json.dumps(data, separators=(",", ":"), allow_nan=False).encode()
            except (ValueError, OSError, KeyError):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--port", type=int, default=19491)
    args = parser.parse_args()
    serve(args.port, Projection(args.runs, json.loads(args.baseline.read_text())))
