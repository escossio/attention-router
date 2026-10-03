#!/usr/bin/env python3
"""Read-only end-to-end message tracer for Andy Ops."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import subprocess
from typing import Any


DB_CONTAINER = os.environ.get("ANDY_OPS_TRACE_DB_CONTAINER", "").strip()
DB_USER = os.environ.get("ANDY_OPS_TRACE_DB_USER", "attention_router")
DB_NAME = os.environ.get("ANDY_OPS_TRACE_DB_NAME", "attention_router")
TRACE_LIMIT = max(1, min(int(os.environ.get("ANDY_OPS_TRACE_LIMIT", "8")), 50))


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    return value.astimezone().isoformat() if value else None


def _delta_ms(start: datetime | None, end: datetime | None) -> int | None:
    if not start or not end:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def _psql_json(sql: str) -> list[dict[str, Any]]:
    if not DB_CONTAINER:
        raise RuntimeError("TRACE_DB_CONTAINER_NOT_CONFIGURED")
    cmd = [
        "docker", "exec", DB_CONTAINER,
        "psql", "-U", DB_USER, "-d", DB_NAME,
        "-At", "-c", sql,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=2.0,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("TRACE_DB_QUERY_FAILED")
    raw = result.stdout.strip() or "[]"
    payload = json.loads(raw)
    return payload if isinstance(payload, list) else []


TRACE_SQL = r"""
select coalesce(json_agg(row_to_json(t) order by t.received_at desc), '[]'::json)::text
from (
  select
    i.id,
    i.correlation_id,
    i.source,
    i.event_type,
    i.status,
    i.received_at,
    i.processed_at,
    i.interaction_id,
    coalesce((
      select json_agg(json_build_object(
        'event_type', a.event_type,
        'origin', a.origin,
        'created_at', a.created_at
      ) order by a.created_at)
      from audit_events a
      where a.correlation_id = i.correlation_id
    ), '[]'::json) as audits,
    coalesce((
      select json_agg(json_build_object(
        'id', q.id,
        'kind', q.kind,
        'status', q.status,
        'created_at', q.created_at,
        'processed_at', q.processed_at
      ) order by q.created_at)
      from queue q
      where q.payload->>'event_id' = i.id
         or q.payload->>'interaction_id' = i.interaction_id
    ), '[]'::json) as queues,
    coalesce((
      select json_agg(json_build_object(
        'id', d.id,
        'decision_type', d.decision_type,
        'recommended_action', d.recommended_action,
        'status', d.status,
        'execution_allowed', d.execution_allowed,
        'external_delivery_allowed', d.external_delivery_allowed,
        'confidence', d.confidence,
        'created_at', d.created_at
      ) order by d.created_at)
      from agent_decisions d
      where d.event_id = i.id
    ), '[]'::json) as decisions,
    coalesce((
      select json_agg(json_build_object(
        'id', e.id,
        'intent_type', e.intent_type,
        'status', e.status,
        'execution_allowed', e.execution_allowed,
        'external_delivery_allowed', e.external_delivery_allowed,
        'release_status', e.release_status,
        'blocked_reason', e.blocked_reason,
        'created_at', e.created_at,
        'released_at', e.released_at,
        'executed_at', e.executed_at
      ) order by e.created_at)
      from agent_execution_intents e
      join agent_decisions d on d.id = e.agent_decision_id
      where d.event_id = i.id
    ), '[]'::json) as intents,
    coalesce((
      select json_agg(json_build_object(
        'id', o.id,
        'status', o.status,
        'destination', o.destination,
        'created_at', o.created_at,
        'claimed_at', o.claimed_at,
        'completed_at', o.completed_at,
        'attempt_count', o.attempt_count,
        'has_error', (o.last_error is not null and o.last_error <> '')
      ) order by o.created_at)
      from outbox_messages o
      where o.correlation_id = i.correlation_id
    ), '[]'::json) as outbox
  from inbound_events i
  order by i.received_at desc
  limit {limit}
) t
""".format(limit=TRACE_LIMIT)


def _event_time(events: list[dict[str, Any]], *types: str) -> datetime | None:
    wanted = set(types)
    matches = [
        _dt(str(item.get("created_at") or ""))
        for item in events
        if item.get("event_type") in wanted
    ]
    values = [item for item in matches if item]
    return max(values) if values else None


def _event_present(events: list[dict[str, Any]], *types: str) -> bool:
    wanted = set(types)
    return any(item.get("event_type") in wanted for item in events)


def _state_from_status(value: str | None) -> str:
    status = str(value or "").upper()
    if status in {"DONE", "SENT", "PROCESSED", "COMPLETED", "RELEASED"}:
        return "DONE"
    if status in {"FAILED", "ERROR", "DEAD", "BLOCKED", "QUARANTINED"}:
        return "FAILED"
    if status in {"PENDING", "PROCESSING", "CLAIMED", "HELD", "STARTED"}:
        return "ACTIVE"
    return "UNKNOWN"


def _stage(
    key: str,
    label: str,
    state: str,
    at: datetime | None,
    started: datetime | None,
    detail: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "state": state,
        "at": _iso(at),
        "elapsed_ms": _delta_ms(started, at),
        "detail": detail,
    }


def _build_trace(row: dict[str, Any]) -> dict[str, Any]:
    audits = row.get("audits") or []
    queues = row.get("queues") or []
    decisions = row.get("decisions") or []
    intents = row.get("intents") or []
    outbox = row.get("outbox") or []

    started = _dt(str(row.get("received_at") or ""))
    processed = _dt(str(row.get("processed_at") or ""))
    ignored = _event_present(audits, "inbound_from_me_ignored")

    ingress_at = _event_time(audits, "internal_event_accepted") or processed
    grace_released = _event_time(audits, "grace.released")
    grace_claimed = _event_time(audits, "grace.claimed")
    grace_active = _event_time(audits, "grace.opened", "grace.extended")
    control_at = grace_released or grace_claimed or grace_active

    queue = queues[-1] if queues else None
    decision = decisions[-1] if decisions else None
    intent = intents[-1] if intents else None
    outbound = outbox[-1] if outbox else None


    inbound_state = "DONE" if str(row.get("status") or "").upper() == "PROCESSED" else _state_from_status(row.get("status"))
    stages = [
        _stage(
            "inbound", "INBOUND", inbound_state,
            started, started, str(row.get("source") or "unknown"),
        ),
        _stage(
            "ingress", "INGRESS",
            "DONE" if ingress_at else ("FAILED" if inbound_state == "FAILED" else "WAITING"),
            ingress_at, started,
            "accepted" if ingress_at else None,
        ),
    ]

    if ignored:
        control_state, control_detail = "SKIPPED", "from_me ignored"
    elif grace_released:
        control_state, control_detail = "DONE", "grace released"
    elif grace_claimed:
        control_state, control_detail = "ACTIVE", "grace claimed"
    elif grace_active:
        control_state, control_detail = "WAITING", "owner grace"
    else:
        control_state, control_detail = ("DONE", "direct") if (queue or decision) else ("WAITING", None)
    stages.append(_stage("control", "CONTROL", control_state, control_at, started, control_detail))


    if queue:
        q_state = _state_from_status(queue.get("status"))
        q_at = _dt(str(queue.get("processed_at") or queue.get("created_at") or ""))
        q_detail = f"{queue.get('kind') or 'queue'} / {queue.get('status') or 'unknown'}"
    else:
        q_state = "SKIPPED" if ignored else ("DONE" if decision else "WAITING")
        q_at = None
        q_detail = "no queue record" if decision else None
    stages.append(_stage("queue", "QUEUE", q_state, q_at, started, q_detail))

    if decision:
        d_at = _dt(str(decision.get("created_at") or ""))
        d_detail = " / ".join(
            str(x) for x in (
                decision.get("decision_type"),
                decision.get("recommended_action"),
                decision.get("status"),
            ) if x
        )
        d_state = "DONE"
    else:
        d_at, d_detail = None, None
        d_state = "SKIPPED" if ignored else "WAITING"
    stages.append(_stage("decision", "DECISION", d_state, d_at, started, d_detail))


    if intent:
        i_at = _dt(str(
            intent.get("executed_at")
            or intent.get("released_at")
            or intent.get("created_at")
            or ""
        ))
        if intent.get("blocked_reason"):
            i_state = "BLOCKED"
        elif str(intent.get("status") or "").upper() in {"SENT", "DONE", "COMPLETED"}:
            i_state = "DONE"
        elif str(intent.get("release_status") or "").upper() == "RELEASED":
            i_state = "DONE"
        else:
            i_state = "ACTIVE"
        i_detail = " / ".join(
            str(x) for x in (
                intent.get("intent_type"),
                intent.get("status"),
                intent.get("release_status"),
            ) if x
        )
    else:
        i_at, i_detail = None, None
        i_state = "SKIPPED" if ignored else "WAITING"
    stages.append(_stage("execution", "EXECUTION", i_state, i_at, started, i_detail))


    if outbound:
        o_at = _dt(str(outbound.get("completed_at") or outbound.get("claimed_at") or outbound.get("created_at") or ""))
        o_state = _state_from_status(outbound.get("status"))
        o_detail = f"{outbound.get('destination') or 'outbox'} / {outbound.get('status') or 'unknown'}"
    else:
        o_at, o_detail = None, None
        o_state = "SKIPPED" if ignored else "WAITING"
    stages.append(_stage("outbox", "OUTBOX", o_state, o_at, started, o_detail))

    if outbound and str(outbound.get("status") or "").upper() == "DONE":
        sent_at = _dt(str(outbound.get("completed_at") or outbound.get("created_at") or ""))
        sent_state, sent_detail = "DONE", "delivered"
    elif outbound and _state_from_status(outbound.get("status")) == "FAILED":
        sent_at = o_at
        sent_state, sent_detail = "FAILED", "delivery failed"
    else:
        sent_at = None
        sent_state = "SKIPPED" if ignored else "WAITING"
        sent_detail = None
    stages.append(_stage("outbound", "OUTBOUND", sent_state, sent_at, started, sent_detail))


    if ignored:
        overall = "IGNORED"
    elif sent_state == "DONE":
        overall = "COMPLETED"
    elif sent_state == "FAILED" or o_state == "FAILED":
        overall = "FAILED"
    elif i_state == "BLOCKED":
        overall = "BLOCKED"
    elif queue and _state_from_status(queue.get("status")) == "ACTIVE":
        overall = "WAITING_WORKER"
    elif grace_active and not grace_released:
        overall = "WAITING_GRACE"
    elif decision:
        overall = "DECIDED"
    elif ingress_at:
        overall = "INGESTED"
    else:
        overall = "RECEIVED"

    terminal_at = sent_at if overall in {"COMPLETED", "FAILED"} else None
    now = datetime.now(timezone.utc)
    unresolved = next(
        (stage for stage in stages if stage["state"] in {"FAILED", "BLOCKED", "ACTIVE", "WAITING"}),
        None,
    )
    current = unresolved["label"] if unresolved else next(
        (stage["label"] for stage in reversed(stages) if stage["state"] != "SKIPPED"),
        "INBOUND",
    )
    return {
        "correlation_id": row.get("correlation_id"),
        "correlation_short": str(row.get("correlation_id") or "")[:12],
        "inbound_event_id": row.get("id"),
        "source": row.get("source"),
        "event_type": row.get("event_type"),
        "received_at": _iso(started),
        "overall_state": overall,
        "current_stage": current,
        "duration_ms": _delta_ms(started, terminal_at),
        "age_ms": _delta_ms(started, now),
        "stages": stages,
    }


def sample_message_traces() -> dict[str, Any]:
    try:
        rows = _psql_json(TRACE_SQL)
        traces = [_build_trace(row) for row in rows]
        return {
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "ok": True,
            "error": None,
            "traces": traces,
        }
    except Exception as error:
        return {
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "ok": False,
            "error": type(error).__name__,
            "traces": [],
        }