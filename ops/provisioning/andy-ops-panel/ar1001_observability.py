"""AR-1001: bounded read-only Zabbix projection for the existing MGMT service.

NOT a new collector, trigger, incident engine, SDK tool, or Zabbix writer.
No conversation payloads, actor IDs, private inventory or tokens.
"""
from __future__ import annotations

from datetime import datetime, timezone
import subprocess
import threading
import time

MAX_SAMPLE_AGE = 360  # Zabbix sender schedule: 120s
CACHE_SECONDS = 30
EXPECTED_KEYS = (
    "andy.reply.probe_ok",
    "andy.reply.functional_stalled",
    "andy.reply.direct_6h",
    "andy.reply.no_safe_6h",
    "andy.reply.blueprint_6h",
    "andy.reply.other_failed_6h",
    "andy.reply.latest_failure_epoch",
    "andy.reply.last_agent_done_epoch",
    "andy.reply.direct_event_6h",
    "andy.reply.direct_processed_6h",
    "andy.reply.decision_total_6h",
    "andy.reply.decisions_dry_run_6h",
    "andy.reply.outbox_new_6h",
    "andy.reply.grace_released_6h",
    "andy.reply.grace_canceled_6h",
)
ALLOWED_TRIGGER_NAMES = (
    "ANDY_WA_AUTO_REPLY_STALLED",
    "ANDY_WA_REPLY_OBSERVABILITY_LOST",
)
SQL = """
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '2s';
SELECT 'M|' || i.key_ || '|' || h.clock::text || '|' || h.value::text
FROM items AS i
JOIN LATERAL (
    SELECT clock, value FROM history_uint
    WHERE itemid = i.itemid ORDER BY clock DESC LIMIT 1
) AS h ON true
WHERE i.key_ IN (
    'andy.reply.probe_ok',
    'andy.reply.functional_stalled',
    'andy.reply.direct_6h',
    'andy.reply.no_safe_6h',
    'andy.reply.blueprint_6h',
    'andy.reply.other_failed_6h',
    'andy.reply.latest_failure_epoch',
    'andy.reply.last_agent_done_epoch',
    'andy.reply.direct_event_6h',
    'andy.reply.direct_processed_6h',
    'andy.reply.decision_total_6h',
    'andy.reply.decisions_dry_run_6h',
    'andy.reply.outbox_new_6h',
    'andy.reply.grace_released_6h',
    'andy.reply.grace_canceled_6h'
) AND i.status = 0
ORDER BY i.key_;
SELECT 'T|' || split_part(t.description, ':', 1) || '|' || t.value::text
FROM triggers AS t
WHERE t.status = 0
  AND (t.description LIKE 'ANDY_WA_AUTO_REPLY_STALLED:%'
       OR t.description LIKE 'ANDY_WA_REPLY_OBSERVABILITY_LOST:%')
ORDER BY t.description;
COMMIT;
"""
# Query only the already operational Zabbix ROC store, not the Attention Router DB.
DB_CMD = (
    "docker", "exec", "-i", "-u", "postgres", "andy-roc-zabbix-db",
    "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "zabbix",
    "-d", "zabbix", "-Atq",
)
_lock = threading.Lock()
_cache = None
_cache_mono = 0.0


def _stamp(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _facet(dimension: str, state: str, reason: str, *, coverage: str = "NONE") -> dict:
    return {
        "dimension": dimension, "state": state, "reason_code": reason,
        "coverage": coverage,
    }


def project(raw: str, *, now: int) -> dict:
    """Parse a fixed allowlist. Never accept arbitrary Zabbix content into JSON."""
    if type(raw) is not str or len(raw) > 4096:
        raise ValueError("AR1001_BOUNDED_OUTPUT_REQUIRED")
    values = {}
    triggers = {}
    for line in raw.splitlines():
        if not line:
            continue
        parts = line.split("|")
        if len(parts) == 4 and parts[0] == "M":
            _, key, clock, number = parts
            if key not in EXPECTED_KEYS or key in values:
                raise ValueError("AR1001_UNKNOWN_OR_DUPLICATE_KEY")
            if not clock.isascii() or not clock.isdecimal() or not number.isascii() or not number.isdecimal():
                raise ValueError("AR1001_INVALID_NUMBER")
            clock_int, value_int = int(clock), int(number)
            if not 0 <= value_int <= 9223372036854775807 or not 0 < clock_int <= now + 30:
                raise ValueError("AR1001_OUT_OF_RANGE")
            values[key] = {"value": value_int, "sampled_at": _stamp(clock_int),
                           "age_seconds": max(0, now - clock_int),
                           "fresh": 0 <= now - clock_int <= MAX_SAMPLE_AGE}
        elif len(parts) == 3 and parts[0] == "T":
            _, key, status = parts
            if key not in ALLOWED_TRIGGER_NAMES or key in triggers or status not in ("0", "1"):
                raise ValueError("AR1001_INVALID_TRIGGER")
            triggers[key] = int(status)
        else:
            raise ValueError("AR1001_UNEXPECTED_DATA")
    if set(triggers) != set(ALLOWED_TRIGGER_NAMES):
        raise ValueError("AR1001_TRIGGER_DATA_MISSING")

    healthy_probe = values.get("andy.reply.probe_ok")
    stalled = values.get("andy.reply.functional_stalled")
    usable = bool(healthy_probe and healthy_probe["fresh"] and healthy_probe["value"] == 1)
    functional_fresh = bool(stalled and stalled["fresh"] and usable)
    if functional_fresh and stalled["value"] not in (0, 1):
        raise ValueError("AR1001_INVALID_FUNCTIONAL_FLAG")

    if not usable:
        # An unhealthy/stale probe cannot certify other Zabbix metrics as current.
        for metric_key, metric in values.items():
            if metric_key != "andy.reply.probe_ok":
                metric["fresh"] = False
        state = "UNKNOWN"
        cause = "PROBE_STALE_OR_UNAVAILABLE"
    elif not functional_fresh:
        state = "UNKNOWN"
        cause = "FUNCTIONAL_SIGNAL_STALE"
    elif stalled["value"] == 1:
        state = "OBSERVED_FAILURE"
        cause = "REPLY_FUNCTIONAL_STALLED"
    else:
        state = "NO_RELATED_ANOMALY"
        cause = "NO_ACTIVE_FUNCTIONAL_STALL"

    facets = [
        _facet("collector_integrity", "OBSERVED_HEALTHY" if usable else "UNKNOWN",
               "PROBE_FRESH" if usable else "PROBE_STALE_OR_UNAVAILABLE",
               coverage="GENERAL_HEALTH" if usable else "NONE"),
        _facet("physical", "NOT_OBSERVED", "NO_INCIDENT_LINK_MEASUREMENT"),
        _facet("data_link", "NOT_OBSERVED", "NO_INCIDENT_VLAN_MEASUREMENT"),
        _facet("network", "NOT_OBSERVED", "NO_CORRELATED_IP_TEST"),
        _facet("transport", "NOT_OBSERVED", "NO_CORRELATED_TCP_TEST"),
        _facet("runtime", "NOT_OBSERVED", "NO_INCIDENT_RUNTIME_EVIDENCE"),
        _facet("application_http", "NOT_OBSERVED", "NO_CORRELATED_HTTP_MEASUREMENT"),
        _facet("tracing", "NOT_OBSERVED", "NO_MATCHED_OTEL_TRACE"),
        _facet("application_flow", state, cause,
               coverage="OBSERVED_PATH" if functional_fresh else "NONE"),
        _facet("persistence", "NOT_OBSERVED", "NO_INCIDENT_DB_OPERATION_EVIDENCE"),
        _facet("delivery", "UNKNOWN", "DELIVERY_EXPECTATION_NOT_CERTIFIED"),
    ]
    return {
        "code": "AR-1001",
        "title": "Andy não produz resposta automática esperada",
        "status": state,
        "reason_code": cause,
        "observed_at": _stamp(now),
        "source": "ROC_ZABBIX_READONLY",
        "window": "6h",
        "sample_max_age_seconds": MAX_SAMPLE_AGE,
        "metrics": values,
        "trigger_states": triggers,
        "facets": facets,
        "note": ("Valores são observações da sonda Zabbix. Estados DRY_RUN e ausência "
                 "de outbox não provam causa; faltam elegibilidade e correlação "
                 "OSI/OTel específicas do incidente."),
    }


def sample(*, run=subprocess.run, now_fn=time.time) -> dict:
    now = int(now_fn())
    try:
        result = run(DB_CMD, input=SQL, text=True, capture_output=True,
                     timeout=4, check=True)
        return project(result.stdout, now=now)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {
            "code": "AR-1001", "status": "UNKNOWN",
            "reason_code": "ZABBIX_OBSERVATION_UNAVAILABLE",
            "observed_at": _stamp(now), "metrics": {}, "trigger_states": {},
            "facets": [_facet("collector_integrity", "UNKNOWN", "ZABBIX_OBSERVATION_UNAVAILABLE")],
            "source": "ROC_ZABBIX_READONLY",
            "note": "Falha da consulta; nenhum valor foi presumido.",
        }


def cached_sample() -> dict:
    global _cache, _cache_mono
    with _lock:
        stamp = time.monotonic()
        if _cache is None or stamp - _cache_mono >= CACHE_SECONDS:
            _cache = sample()
            _cache_mono = stamp
        return _cache.copy()
