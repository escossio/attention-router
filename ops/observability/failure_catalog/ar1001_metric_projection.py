"""Pure, fail-closed mapping of AR-1001's single integrated readonly SQL row.

Not a new collector, process, scheduler, service or Zabbix sender.
The existing reply_health.py owner should call the parser, apply the existing
sticky incident state machine, then publish new and legacy keys atomically.
"""
from __future__ import annotations

LEGACY_FIELDS = (
    "direct_6h",
    "no_safe_6h",
    "blueprint_6h",
    "other_failed_6h",
    "latest_failure_ts",
    "last_agent_delivery_ts",
    "latest_direct_ts",
)

ENRICHED_KEYS = (
    "andy.reply.direct_event_6h",
    "andy.reply.direct_processed_6h",
    "andy.reply.decision_total_6h",
    "andy.reply.decisions_dry_run_6h",
    "andy.reply.outbox_new_6h",
    "andy.reply.grace_released_6h",
    "andy.reply.grace_canceled_6h",
)

MAX_INT = 9223372036854775807


def parse_integrated_psql_row(output: str) -> tuple[dict[str, int], dict[str, int]]:
    """Return existing evidence naming and new Zabbix item metrics.

    Reject incomplete/ambiguous output; caller must publish probe_ok=0 on
    failure, preserving prior incident state. Never output raw input in errors.
    """
    if type(output) is not str or len(output) > 1024:
        raise ValueError("AR1001_ROW_INVALID")
    rows = [line.strip() for line in output.splitlines() if line.strip()]
    if len(rows) != 1:
        raise ValueError("AR1001_ROW_INVALID")
    values = rows[0].split("|")
    if len(values) != 14:
        raise ValueError("AR1001_ROW_INVALID")
    parsed = []
    for value in values:
        if not value or not value.isascii() or not value.isdecimal():
            raise ValueError("AR1001_FIELD_INVALID")
        number = int(value)
        if number > MAX_INT:
            raise ValueError("AR1001_FIELD_OUT_OF_BOUNDS")
        parsed.append(number)
    legacy = dict(zip(LEGACY_FIELDS, parsed[:7]))
    enrichment = dict(zip(ENRICHED_KEYS, parsed[7:]))
    if enrichment["andy.reply.direct_processed_6h"] > enrichment["andy.reply.direct_event_6h"]:
        raise ValueError("AR1001_COUNTS_INCOHERENT")
    if enrichment["andy.reply.decisions_dry_run_6h"] > enrichment["andy.reply.decision_total_6h"]:
        raise ValueError("AR1001_COUNTS_INCOHERENT")
    return legacy, enrichment
