"""Read-only, allowlisted status projection for the existing protected operations UI."""

from __future__ import annotations
import json
import math
import os
import re
import stat
import time
from pathlib import Path

SOURCES = {
    "mirror": ("tcp-brain-capture/status.json", "tcp-brain.capture-status.v1"),
    "trunk": ("tcp-brain-capture-trunk/status.json", "tcp-brain.capture-status.v1"),
    "flow": ("tcp-brain-flow/status.json", "tcp-brain.flow-status.v1"),
}
MAX_BYTES = 262144
MAX_AGE_NS = 5_000_000_000
CAPTURE_COUNTS = (
    "packets_received",
    "events_emitted",
    "socket_drop_count",
    "packet_statistics_drops",
    "interface_rx_drop_delta",
    "interface_rx_missed_delta",
    "parse_errors",
    "subscriber_drops",
    "subscriber_backpressure",
    "subscriber_partial_writes",
    "subscriber_queue_overflows",
    "subscriber_peer_closed",
    "subscriber_write_errors",
    "subscriber_shutdowns",
    "subscriber_rejected",
    "subscriber_queue_records_discarded",
    "subscriber_queued_bytes",
    "subscriber_queue_peak_bytes",
)
FLOW_COUNTS = (
    "input_events",
    "tcp_packets",
    "deduplicated_packets",
    "active_flows",
    "established_total",
    "forward_ok_total",
    "forward_missing_total",
    "forward_pending",
    "anomalies_total",
    "observation_inconclusive_total",
)
CONTINUITY_COUNTS = (
    "gaps",
    "epoch_changes",
    "out_of_order",
    "unverified",
    "disconnects",
    "invalid_records",
)


def count(value):
    return value if type(value) is int and 0 <= value < 2**53 else None


def number(value):
    return (
        value
        if type(value) in (int, float) and 0 <= value < 2**53 and math.isfinite(value)
        else None
    )


def read_status(path, expected, now_ns):
    age = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
                return None, "INVALID", None
            age_ns = now_ns - info.st_mtime_ns
            if age_ns < -1_000_000_000:
                return None, "INVALID", None
            age = round(max(age_ns, 0) / 1e9, 3)
            if age_ns > MAX_AGE_NS:
                return None, "STALE", age
            raw = handle.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return None, "INVALID", age
        data = json.loads(raw)
    except OSError:
        return None, "UNAVAILABLE", age
    except (ValueError, UnicodeError, RecursionError):
        return None, "INVALID", age
    if not isinstance(data, dict) or data.get("schema_version") != expected:
        return None, "INVALID", age
    epoch = data.get("started_timestamp_ns")
    if type(epoch) is not int or not 0 < epoch <= now_ns + 1_000_000_000:
        return None, "INVALID", age
    return data, "FRESH", age


def basic(data, status, age, now_ns):
    row = {
        "telemetry": status,
        "status_age_seconds": age,
        "uptime_seconds": None,
        "last_activity_seconds": None,
        "source_epoch": None,
    }
    if data:
        row["source_epoch"] = str(data["started_timestamp_ns"])
        row["uptime_seconds"] = max(0, (now_ns - data["started_timestamp_ns"]) // 1_000_000_000)
        # The sample's age must be included; freshness at emission is not live freshness.
        v = number(data.get("freshness_seconds"))
        row["last_activity_seconds"] = (
            round(v + age, 3) if v is not None and age is not None else None
        )
    return row


def capture_projection(data, status, age, now_ns):
    row = basic(data, status, age, now_ns)
    d = data or {}
    row.update({k: count(d.get(k)) for k in CAPTURE_COUNTS})
    row.update({k: number(d.get(k)) for k in ("current_bps", "peak_bps", "link_speed_mbps")})
    row["carrier"] = d["carrier"] if type(d.get("carrier")) is bool else None
    row["capture_health"] = "UNKNOWN"
    row["delivery_health"] = "UNKNOWN"
    row["mode"] = "UNAVAILABLE"
    if not data:
        return row
    row["mode"] = "V2_COUNTERS" if "subscriber_queue_overflows" in d else "LEGACY"
    drops = [
        row[k]
        for k in ("socket_drop_count", "interface_rx_drop_delta", "interface_rx_missed_delta")
    ]
    if row["mode"] == "V2_COUNTERS" or row["packet_statistics_drops"] is not None:
        drops.append(row["packet_statistics_drops"])
    if row["carrier"] is False:
        row["capture_health"] = "NO_CARRIER"
    elif any(v is not None and v > 0 for v in drops):
        row["capture_health"] = "DROPS_RECORDED"
    elif row["carrier"] is True and all(v == 0 for v in drops) and row["parse_errors"] == 0:
        row["capture_health"] = "NO_RECORDED_DROPS"
    if row["subscriber_drops"] is not None and row["subscriber_drops"] > 0:
        row["delivery_health"] = "ISSUES_RECORDED"
    elif row["mode"] == "LEGACY":
        row["delivery_health"] = "UNVERIFIED"
    elif any(
        (row[k] or 0) > 0
        for k in (
            "subscriber_queue_overflows",
            "subscriber_write_errors",
            "subscriber_queue_records_discarded",
        )
    ):
        row["delivery_health"] = "ISSUES_RECORDED"
    elif row["subscriber_drops"] == 0 and all(
        row[k] == 0
        for k in (
            "subscriber_queue_overflows",
            "subscriber_write_errors",
            "subscriber_queue_records_discarded",
        )
    ):
        if row["subscriber_backpressure"] is not None:
            row["delivery_health"] = (
                "BACKPRESSURE_RECORDED"
                if row["subscriber_backpressure"] > 0
                else "NO_ERRORS_RECORDED"
            )
    return row


def source_set(value):
    if not isinstance(value, list) or any(
        type(v) is not str or v not in ("mirror", "trunk") for v in value
    ):
        return None
    return sorted(set(value))


def flow_projection(data, status, age, now_ns, captures):
    row = basic(data, status, age, now_ns)
    d = data or {}
    row.update({k: count(d.get(k)) for k in FLOW_COUNTS})
    row.update(
        {
            "inputs_configured": source_set(d.get("inputs_configured")),
            "inputs_connected": source_set(d.get("inputs_connected")),
            "observation_health": "UNKNOWN",
            "continuity": {},
            "coverage": {},
            "forward_pairs": None,
        }
    )
    if not data:
        return row
    pairs = d.get("forward_ok_by_vlan")
    if isinstance(pairs, dict) and len(pairs) <= 256:
        projected = []
        for key, value in pairs.items():
            if (
                type(key) is not str
                or not re.fullmatch(r"[0-9]{1,4}->[0-9]{1,4}", key)
                or count(value) is None
            ):
                projected = None
                break
            a, b = map(int, key.split("->"))
            if not 1 <= a <= 4094 or not 1 <= b <= 4094:
                projected = None
                break
            projected.append({"from_vlan": a, "to_vlan": b, "count": value})
        if projected is not None:
            row["forward_pairs"] = sorted(projected, key=lambda x: (x["from_vlan"], x["to_vlan"]))
    if row["inputs_connected"] is None or row["inputs_configured"] is None:
        return row
    if row["inputs_connected"] != ["mirror", "trunk"] or row["inputs_configured"] != [
        "mirror",
        "trunk",
    ]:
        row["observation_health"] = "INCOMPLETE_INPUTS"
        return row
    if any(captures[n]["telemetry"] != "FRESH" for n in ("mirror", "trunk")):
        row["observation_health"] = "UNKNOWN"
        return row
    if "observation_state" not in d:
        row["observation_health"] = "LEGACY_UNVERIFIED"
        return row
    cov = d.get("coverage")
    cont = d.get("stream_continuity")
    if not isinstance(cov, dict) or not isinstance(cont, dict):
        row["observation_health"] = "INCONCLUSIVE"
        return row
    for n in ("mirror", "trunk"):
        c = cov.get(n)
        s = cont.get(n)
        if not isinstance(c, dict) or not isinstance(s, dict):
            row["observation_health"] = "INCONCLUSIVE"
            return row
        w = c.get("watermark_ns")
        start = c.get("start_ns")
        fresh = (
            captures[n]["mode"] == "V2_COUNTERS"
            and captures[n]["carrier"] is True
            and s.get("epoch") == captures[n]["source_epoch"]
            and c.get("fresh") is True
            and type(w) is int
            and type(start) is int
            and 0 < start <= w <= now_ns
            and now_ns - w <= 1_000_000_000
        )
        row["coverage"][n] = {
            "fresh": fresh,
            "watermark_age_seconds": round((now_ns - w) / 1e9, 3)
            if type(w) is int and 0 < w <= now_ns
            else None,
        }
        row["continuity"][n] = {k: count(s.get(k)) for k in CONTINUITY_COUNTS}
    if (
        d.get("observation_state") == "BOUNDED_COVERAGE"
        and all(c["fresh"] for c in row["coverage"].values())
        and all(all(v is not None for v in s.values()) for s in row["continuity"].values())
    ):
        row["observation_health"] = "BOUNDED_COVERAGE"
    else:
        row["observation_health"] = "INCONCLUSIVE"
    return row


def sample_tcpbrain(root=Path("/run"), now_ns=None):
    now_ns = time.time_ns() if now_ns is None else now_ns
    sources = {}
    for name in ("mirror", "trunk"):
        rel, expected = SOURCES[name]
        sources[name] = capture_projection(*read_status(root / rel, expected, now_ns), now_ns)
    rel, expected = SOURCES["flow"]
    flow = flow_projection(*read_status(root / rel, expected, now_ns), now_ns, sources)
    return {
        "schema_version": "tcpbrain.mgmt.v1",
        "sampled_at_ms": now_ns // 1_000_000,
        "sources": sources,
        "flow": flow,
        "scope": "STATUS_ONLY",
        "counters_are_cumulative": True,
    }
