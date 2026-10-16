"""Bounded, opt-in Investigator event journal for read-only Andy Ops.

This is operational metadata, not model reasoning, raw evidence or audit authority.
The operator supplies a protected absolute path, outside Git and web roots.
No journal configuration means no journal writes or hidden dependencies.
"""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import threading

_ID = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
_EVENTS = frozenset({
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
_MAX_BYTES = 2_000_000


class InvestigatorJournal:
    """Emit only whitelisted fields; refuse unsafe inputs without logging them."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("INVESTIGATOR_JOURNAL_ABSOLUTE_PATH_REQUIRED")
        self._lock = threading.Lock()

    def emit(
        self, run_id: str, event: str, *,
        source: str | None = None,
        operation: str | None = None,
        ordinal: int | None = None,
    ) -> None:
        if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
            raise ValueError("INVESTIGATOR_JOURNAL_INVALID_RUN_ID")
        if event not in _EVENTS:
            raise ValueError("INVESTIGATOR_JOURNAL_INVALID_EVENT")
        if source is not None and source not in _SOURCES:
            raise ValueError("INVESTIGATOR_JOURNAL_INVALID_SOURCE")
        if operation is not None and operation not in _OPERATIONS:
            raise ValueError("INVESTIGATOR_JOURNAL_INVALID_OPERATION")
        if ordinal is not None and (type(ordinal) is not int or not 1 <= ordinal <= 256):
            raise ValueError("INVESTIGATOR_JOURNAL_INVALID_ORDINAL")
        data: dict[str, object] = {
            "v": 1, "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id, "event": event,
        }
        if source is not None:
            data["source"] = source
        if operation is not None:
            data["operation"] = operation
        if ordinal is not None:
            data["ordinal"] = ordinal
        record = (json.dumps(data, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        with self._lock:
            fd = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                mode = os.fstat(fd)
                if not os.path.isfile(self.path):
                    raise ValueError("INVESTIGATOR_JOURNAL_NOT_REGULAR")
                if mode.st_size + len(record) > _MAX_BYTES:
                    os.ftruncate(fd, 0)
                if os.write(fd, record) != len(record):
                    raise OSError("INVESTIGATOR_JOURNAL_SHORT_WRITE")
            finally:
                os.close(fd)
