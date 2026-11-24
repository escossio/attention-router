"""Read-only, bounded view of Investigator's private metadata JSONL journal.

Never execute tools or display raw command arguments, model content or source
payloads. The presence of a Docker container is not proof of investigation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat

_ALLOWED_EVENTS = frozenset({
    "started", "heartbeat", "query_requested", "query_observed",
    "query_denied", "query_failed", "query_limited", "completed",
    "failed", "limited", "blocked", "cancelled", "timeout",
})
_SOURCES = frozenset({
    "zabbix", "tempo", "distributed_ci", "linux", "postgresql", "afo_catalog",
})
_OPERATIONS = frozenset({
    "inspect", "read_event", "read_history", "read_trace", "read_jobs",
})
_TERMINAL = frozenset({"completed", "failed", "limited", "blocked", "cancelled", "timeout"})
_ID = re.compile(r"^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$")
_RECORDED_FIELDS = frozenset({"v", "ts", "run_id", "event", "source", "operation", "ordinal"})
_TAIL_BYTES = 65_536
_MAX_EVENTS = 32
_STALE_SECONDS = 20


def _project(record: object, now: datetime) -> dict[str, object] | None:
    if type(record) is not dict or not {"v", "ts", "run_id", "event"} <= record.keys():
        return None
    if not set(record) <= _RECORDED_FIELDS:
        return None
    if type(record["v"]) is not int or record["v"] != 1:
        return None
    if type(record["run_id"]) is not str or not _ID.fullmatch(record["run_id"]):
        return None
    if type(record["event"]) is not str or record["event"] not in _ALLOWED_EVENTS:
        return None
    if type(record["ts"]) is not str or len(record["ts"]) > 40:
        return None
    try:
        timestamp = datetime.fromisoformat(record["ts"].replace("Z", "+00:00"))
        if timestamp.utcoffset() is None or timestamp > now + timedelta(seconds=5):
            return None
    except ValueError:
        return None
    output = {
        "timestamp": timestamp.isoformat(),
        "run_id": record["run_id"],
        "event": record["event"],
    }
    source, operation = record.get("source"), record.get("operation")
    if source is not None or operation is not None:
        if source not in _SOURCES or operation not in _OPERATIONS:
            return None
        output["source"] = source
        output["operation"] = operation
    ordinal = record.get("ordinal")
    if ordinal is not None:
        if type(ordinal) is not int or not 1 <= ordinal <= 256:
            return None
        output["ordinal"] = ordinal
    return output


def sample_investigator_activity(
    *, enabled: bool | None = None, path: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Bounded no-symlink file read, fail closed, no shell/network/Docker access."""
    if enabled is None:
        enabled = os.environ.get("ANDY_OPS_INVESTIGATOR_OBSERVABILITY_ENABLED", "false").lower() == "true"
    if not enabled:
        return {"enabled": False, "state": "DISABLED", "age_seconds": None, "events": [], "queries": 0}
    now = now or datetime.now(timezone.utc)
    if path is None:
        path = os.environ.get("ANDY_OPS_INVESTIGATOR_JOURNAL", "")
    if not path or not Path(path).is_absolute():
        return {"enabled": True, "state": "NOT_CONFIGURED", "age_seconds": None, "events": [], "queries": 0}
    file_path = Path(path)
    # Never read a journal from the static web root. The HTTP server serves
    # that tree directly; the raw file must stay in private host storage.
    panel_root = Path(__file__).resolve().parent
    if panel_root == file_path or panel_root in file_path.resolve(strict=False).parents:
        return {"enabled": True, "state": "UNAVAILABLE", "age_seconds": None, "events": [], "queries": 0}
    if file_path.is_symlink():
        return {"enabled": True, "state": "UNAVAILABLE", "age_seconds": None, "events": [], "queries": 0}
    try:
        fd = os.open(file_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (info.st_mode & stat.S_IROTH):
                raise ValueError("NOT_PRIVATE_REGULAR_FILE")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                handle.seek(max(info.st_size - _TAIL_BYTES, 0))
                data = handle.read(_TAIL_BYTES)
        finally:
            os.close(fd)
    except FileNotFoundError:
        return {"enabled": True, "state": "AWAITING_EVENTS", "age_seconds": None, "events": [], "queries": 0}
    except (OSError, ValueError):
        return {"enabled": True, "state": "UNAVAILABLE", "age_seconds": None, "events": [], "queries": 0}
    lines = data.splitlines()
    if info.st_size > _TAIL_BYTES:
        lines = lines[1:]
    records = []
    for line in lines[-256:]:
        if len(line) > 512:
            continue
        try:
            projected = _project(json.loads(line), now)
        except (UnicodeError, ValueError, TypeError):
            continue
        if projected:
            records.append(projected)
    if not records:
        return {"enabled": True, "state": "AWAITING_EVENTS", "age_seconds": None, "events": [], "queries": 0}
    run_id = records[-1]["run_id"]
    timeline = [record for record in records if record["run_id"] == run_id][-_MAX_EVENTS:]
    latest = timeline[-1]
    stamp = datetime.fromisoformat(latest["timestamp"])
    age = max(0, int((now - stamp).total_seconds()))
    if latest["event"] in _TERMINAL:
        state = latest["event"].upper()
    elif latest["event"] == "started" and age <= _STALE_SECONDS:
        state = "STARTING"
    elif age <= _STALE_SECONDS:
        state = "RUNNING"
    else:
        state = "STALE"
    queries = len([x for x in timeline if x["event"] == "query_requested"])
    return {
        "enabled": True, "state": state, "age_seconds": age,
        "run_id": run_id, "events": timeline, "queries": queries,
    }
