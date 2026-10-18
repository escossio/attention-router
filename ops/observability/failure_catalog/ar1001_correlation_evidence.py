"""AR-1001 correlation evidence contract. Pure, privacy-preserving.

No PostgreSQL, Tempo, Zabbix, logs or network access. The output does not
expose event, interaction, tenant, trace or correlation identifiers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_FAILURE_TYPES = frozenset({"behavior.unavailable", "conversation.blocked"})
_TRACE_ATTR = "roc.correlation_id"
_KNOWN_SPANS = frozenset({
    "attention.message", "roc.INBOUND", "roc.INGRESS", "roc.CONTROL",
    "roc.QUEUE", "roc.DECISION", "roc.EXECUTION", "andy.message",
    "inbound.receive", "decision.evaluate", "behavior.generate",
})
MAX_TEMPORAL_DELTA_SECONDS = 900


def _valid_uuid(value: object) -> bool:
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def _instant(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


def _public(level: str, reason: str, *, span_names: tuple[str, ...] = ()) -> dict:
    return {
        "contract_version": "ar1001-correlation-evidence-v0",
        "level": level,
        "reason_code": reason,
        "scope": "ONE_INTERACTION_WITH_FAILURE_AUDIT_NOT_ZABBIX_PROBLEM_CAUSE",
        "verified_source": "DURABLE_AUDIT_AND_TEMPO_SPAN_ATTRIBUTES",
        "span_names": list(span_names[:8]),
        "same_failure_operation_proven": level == "SAME_AUDIT_CORRELATION",
        "causal_origin_proven": False,
        "zabbix_event_member_proven": False,
    }


def classify(
    *,
    inbound_source: str,
    inbound_interaction_id: str | None,
    audit_interaction_id: str | None,
    same_tenant: bool,
    failure_event_type: str,
    inbound_correlation_id: str | None,
    audit_correlation_id: str | None,
    fetched_span_correlation_id: str | None,
    fetched_span_attribute_name: str | None,
    fetched_span_name: str | None,
    inbound_at: datetime,
    audit_at: datetime,
    span_at: datetime | None,
    tempo_fetch_succeeded: bool,
) -> dict:
    """Conservative evidence levels, NOT a root cause or incident event join.

    Exact UUID equality must be checked against the fetched span attribute.
    A search hit alone, approximate time or general service health is not proof.
    """
    if (inbound_source != "wwebjs" or not same_tenant
            or not inbound_interaction_id or inbound_interaction_id != audit_interaction_id
            or failure_event_type not in _FAILURE_TYPES):
        return _public("REJECTED_SCOPE", "INTERACTION_TENANT_OR_EVENT_NOT_ELIGIBLE")
    if not (_valid_uuid(inbound_correlation_id) and _valid_uuid(audit_correlation_id)):
        return _public("UNKNOWN", "PRIVATE_CORRELATION_ID_NOT_VERIFIABLE")
    if not tempo_fetch_succeeded:
        return _public("UNKNOWN", "TRACE_RETRIEVAL_NOT_CONFIRMED")
    if (not _valid_uuid(fetched_span_correlation_id)
            or fetched_span_attribute_name != _TRACE_ATTR
            or fetched_span_name not in _KNOWN_SPANS):
        return _public("NOT_PROVEN", "NO_EXACT_SPAN_ATTRIBUTE_PROOF")
    a, b, c = (_instant(inbound_at), _instant(audit_at), _instant(span_at))
    if not a or not b or not c or b < a - timedelta(seconds=MAX_TEMPORAL_DELTA_SECONDS):
        return _public("UNKNOWN", "TIMESTAMP_OR_SEQUENCE_NOT_VERIFIABLE")
    if (fetched_span_correlation_id == audit_correlation_id
            and abs((c - b).total_seconds()) <= MAX_TEMPORAL_DELTA_SECONDS):
        return _public("SAME_AUDIT_CORRELATION", "EXACT_AUDIT_UUID_IN_FETCHED_SPAN",
                       span_names=(fetched_span_name,))
    if (fetched_span_correlation_id == inbound_correlation_id
            and abs((c - a).total_seconds()) <= MAX_TEMPORAL_DELTA_SECONDS):
        return _public("SAME_INTERACTION_INPUT", "EXACT_INPUT_UUID_ONLY_AUDIT_UUID_DIFFERS",
                       span_names=(fetched_span_name,))
    return _public("NOT_PROVEN", "NO_EXACT_TIME_BOUNDED_MATCH")
