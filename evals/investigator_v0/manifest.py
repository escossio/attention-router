"""Offline synthetic replay manifest; never loads a real agent or provider.

Public cases contain only the event and responses selectable by tool calls.
They must not contain human adjudication or historical answer keys.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

from attention_router.investigator.model import SourceEvent
from attention_router.investigator.simulated import SOURCES, SimulatedConnector


_OPER = re.compile(r"^(?:inspect|read_[a-z0-9_]+)$")
_FORBIDDEN = {"oracle", "root_cause", "correct_answer", "expected", "solution"}


def _validate_no_oracle(value: object) -> None:
    if isinstance(value, dict):
        if any(str(key).lower() in _FORBIDDEN for key in value):
            raise ValueError("REPLAY_ORACLE_EXPOSED")
        for item in value.values():
            _validate_no_oracle(item)
    elif isinstance(value, list):
        for item in value:
            _validate_no_oracle(item)


def load_public_synthetic_case(path: Path) -> tuple[
    SourceEvent, dict[str, SimulatedConnector], dict[str, frozenset[str]]
]:
    """Read a strictly synthetic fixture, without any network or credentials."""
    obj = json.loads(path.read_text(encoding="utf-8"))
    if set(obj) != {"schema_version", "case_id", "seed", "tools"}:
        raise ValueError("REPLAY_TOPLEVEL_FIELDS_INVALID")
    if obj["schema_version"] != 1:
        raise ValueError("REPLAY_SCHEMA_VERSION_UNSUPPORTED")
    if not str(obj["case_id"]).startswith("synthetic-"):
        raise ValueError("REPLAY_NOT_SYNTHETIC")
    _validate_no_oracle(obj)
    seed = obj["seed"]
    if set(seed) != {"source", "original_utf8", "content_type", "tenant_id", "source_event_id"}:
        raise ValueError("REPLAY_SEED_FIELDS_INVALID")
    if not str(seed["source_event_id"]).startswith("synthetic-"):
        raise ValueError("REPLAY_EVENT_NOT_SYNTHETIC")
    if not str(seed["tenant_id"]).startswith("synthetic-"):
        raise ValueError("REPLAY_TENANT_NOT_SYNTHETIC")
    original = seed["original_utf8"].encode("utf-8")
    event = SourceEvent(
        seed["source"], original, seed["content_type"],
        seed["tenant_id"], seed["source_event_id"],
    )
    tools = obj["tools"]
    if not isinstance(tools, dict) or not tools or not set(tools) <= set(SOURCES):
        raise ValueError("REPLAY_TOOLS_INVALID")
    connectors = {}
    allowlist = {}
    for source, operations in tools.items():
        if not isinstance(operations, dict) or not operations:
            raise ValueError("REPLAY_OPERATIONS_INVALID")
        if any(not isinstance(op, str) or _OPER.fullmatch(op) is None for op in operations):
            raise ValueError("REPLAY_OPERATION_NOT_READ_ONLY")
        if any(not isinstance(payload, dict) for payload in operations.values()):
            raise ValueError("REPLAY_OBSERVATION_INVALID")
        connectors[source] = SimulatedConnector(source, operations)
        allowlist[source] = frozenset(operations)
    return event, connectors, allowlist
