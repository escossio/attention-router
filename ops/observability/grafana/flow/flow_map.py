"""Pure, metadata-only Node Graph projection. No IO, credentials or application payload."""

from __future__ import annotations

import json
from collections import defaultdict

MAX_ENTITIES = 64
MAX_OBSERVATIONS = 256
MAX_SPANS = 256


def _item(sample):
    """Accept a Zabbix history sample, not a timeless Docker inspect record."""
    if not isinstance(sample, dict):
        return {}, 0
    try:
        clock = int(sample.get("clock") or 0)
    except (TypeError, ValueError):
        clock = 0
    value = sample.get("value")
    if isinstance(value, str) and len(value) <= 65536:
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = None
    return (value if isinstance(value, dict) else {}), clock


def _fresh(clock, now_s, max_age_s):
    return 0 < clock <= now_s + 30 and now_s - clock <= max_age_s


def _health(sample, now_s, max_age_s):
    value, clock = _item(sample)
    fresh = _fresh(clock, now_s, max_age_s)
    state = str(value.get("state") or "").lower()
    check = str(value.get("health") or "").lower()
    if not value:
        health, reason = "UNKNOWN", "MISSING_EVIDENCE"
    elif not fresh:
        health, reason = "UNKNOWN", "STALE_EVIDENCE"
    elif state in {"dead", "exited"}:
        health, reason = "DOWN", "CONTAINER_NOT_RUNNING"
    elif state != "running":
        health, reason = "UNKNOWN", "DOCKER_STATE_UNVERIFIED"
    elif check == "unhealthy":
        health, reason = "DOWN", "HEALTHCHECK_FAILED"
    elif check == "healthy":
        health, reason = "HEALTHY", "DOCKER_HEALTHCHECK"
    else:
        health, reason = "UNKNOWN", "NO_HEALTHCHECK_EVIDENCE"
    return {
        "status": health,
        "reason": reason,
        "fresh": fresh,
        "observed_at": clock or None,
        "age_seconds": max(0, now_s - clock) if clock else None,
        "last_observed_state": state or None,
        "last_observed_health": check or None,
        "source": "zabbix:andy.docker.container",
    }


def build_flow_map(
    *,
    service_entities,
    container_samples,
    attachment_samples=(),
    expected_edges=(),
    trace_view=None,
    legacy_alerts=(),
    network_events=(),
    now_s,
    max_age_s=180,
    network_sensor_healthy=False,
):
    """Project explicitly mapped entities and verifiable relations for a selected trace.

    service_entities: OTel service name -> Zabbix Docker entity key (explicitly configured).
    container_samples: Zabbix item history rows {clock, value:{entity_key,state,health,...}}.
    attachment_samples: Zabbix item history rows {clock, value:{entity_key,ip,network,...}}.
    expected_edges: declared topological pairs, shown as EXPECTED_ONLY, not network proof.
    trace_view: existing read-only ROC Reader /view JSON.
    network_events: TCP Brain L3/L4 metadata, never raw packets or message bodies.
    """
    if not isinstance(now_s, int) or now_s <= 0 or not 0 < max_age_s <= 3600:
        raise ValueError("invalid observation window")
    if not isinstance(service_entities, dict) or not 0 < len(service_entities) <= MAX_ENTITIES:
        raise ValueError("invalid service mapping")
    if len(set(service_entities.values())) != len(service_entities):
        raise ValueError("ambiguous service-to-entity mapping")
    if not all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in service_entities.items()):
        raise ValueError("invalid service identity")
    if len(container_samples) > MAX_OBSERVATIONS or len(attachment_samples) > MAX_OBSERVATIONS:
        raise ValueError("inventory too large")
    if len(network_events) > MAX_OBSERVATIONS or len(expected_edges) > MAX_OBSERVATIONS:
        raise ValueError("evidence too large")

    samples = {}
    for s in container_samples:
        v, clk = _item(s)
        entity = v.get("entity_key")
        if entity in service_entities.values() and clk > _item(samples.get(entity))[1]:
            samples[entity] = s

    attachments = defaultdict(list)
    for s in attachment_samples:
        v, clk = _item(s)
        if v.get("entity_key") in service_entities.values() and _fresh(clk, now_s, max_age_s):
            ip = v.get("ip")
            if isinstance(ip, str) and ip and len(ip) <= 45:
                attachments[v["entity_key"]].append({"ip": ip, "network": str(v.get("network") or "")[:128]})

    spans = list((trace_view or {}).get("latency") or [])
    if len(spans) > MAX_SPANS:
        raise ValueError("trace too large")
    trace_selected = bool((trace_view or {}).get("metadata"))
    metadata = ((trace_view or {}).get("metadata") or [{}])[0]
    trace_id = metadata.get("trace_id") if trace_selected else None

    hit = set()
    for s in spans:
        if s.get("service") in service_entities:
            hit.add(s["service"])

    nodes, health_overlay, divergences = [], [], []
    ip_to_service = {}
    for service, entity in service_entities.items():
        sample = samples.get(entity)
        val, _ = _item(sample)
        health = _health(sample, now_s, max_age_s)
        ips = attachments[entity] if health["fresh"] else []
        for endpoint in ips:
            ip_to_service.setdefault(endpoint["ip"], service)
        participation = "REACHED" if service in hit else ("NOT_REACHED" if trace_selected else "UNSELECTED")
        node = {
            "id": service, "title": service, "subTitle": str(val.get("name") or entity)[:128],
            "mainStat": health["status"], "secondaryStat": participation,
            "color": (
                "red" if health["status"] == "DOWN" else
                "yellow" if health["status"] == "UNKNOWN" else
                "gray" if participation == "NOT_REACHED" else "green"
            ),
            "detail__health_source": health["source"],
            "detail__evidence": health["reason"],
            "detail__host": str(val.get("host_name") or "")[:64],
            "entity_key": entity, "host": str(val.get("host_name") or "")[:64],
            "interfaces": ips, "health": health, "participation": participation,
        }
        nodes.append(node)
        health_overlay.append({"service": service, **health})
        for alarm in legacy_alerts:
            if not isinstance(alarm, dict) or not alarm.get("active"):
                continue
            if alarm.get("entity_key") == entity and health["status"] == "HEALTHY":
                divergences.append({
                    "entity": service, "kind": "LEGACY_HEALTH_CONFLICT",
                    "source": "zabbix:roc.entity", "trusted_source": health["source"],
                })

    edge_rows = {}
    def edge(source, target, kind, main_stat, secondary_stat):
        if source == target or source not in service_entities or target not in service_entities:
            return
        key = f"{kind}:{source}:{target}"
        edge_rows[key] = {
            "id": key, "source": source, "target": target, "relationship": kind,
            "mainStat": main_stat, "secondaryStat": secondary_stat,
            "detail__relationship": kind,
            "detail__sensor_quality": (
                secondary_stat if kind == "NETWORK_OBSERVED" else "NOT_APPLICABLE"
            ),
            "color": (
                "gray" if kind == "EXPECTED_TOPOLOGY" else
                "orange" if kind == "SPAN_LINK" else
                "cyan" if kind == "PARENT_CHILD" else "yellow"
            ),
            "strokeDasharray": "4,4" if kind == "EXPECTED_TOPOLOGY" else "",
        }

    for pair in expected_edges:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            edge(pair[0], pair[1], "EXPECTED_TOPOLOGY", "EXPECTED_ONLY", "NO_RUNTIME_PROOF")

    # Span IDs are unique only within a trace; inbound and canonical are separate trees.
    span_by_id = {
        (s.get("trace_id"), s.get("span_id")): s
        for s in spans if s.get("span_id") and s.get("service") in service_entities
    }
    trace_overlay = []
    for s in spans:
        if s.get("service") not in service_entities:
            continue
        trace_overlay.append({
            k: s.get(k) for k in (
                "service", "name", "trace_id", "span_id", "parent_span_id",
                "duration_ms", "status", "outcome"
            )
        })
        parent = span_by_id.get((s.get("trace_id"), s.get("parent_span_id")))
        if parent and parent.get("trace_id") == s.get("trace_id"):
            edge(parent["service"], s["service"], "PARENT_CHILD", "REACHED", "OTEL_ONLY")

    if metadata.get("relationship") == "SPAN_LINK" and metadata.get("link_target_status") == "VERIFIED":
        inbound = next(
            (s for s in spans if s.get("span_id") == metadata.get("inbound_span_id")
             and s.get("trace_id") == metadata.get("inbound_trace_id")
             and s.get("name") == "transport.ingress_attempt"), None
        )
        canonical = next((s for s in spans if s.get("name") == "attention.message"
                          and s.get("trace_id") == trace_id), None)
        if inbound and canonical:
            edge(inbound["service"], canonical["service"], "SPAN_LINK", "VERIFIED", "OTEL_ONLY")

    network_evidence = []
    for event in network_events:
        if not isinstance(event, dict):
            continue
        try:
            ts_ns = int(event.get("capture_timestamp_ns") or event.get("first_seen_ns") or 0)
            ts_s = ts_ns // 1_000_000_000
            src_port, dst_port = int(event["src_port"]), int(event["dst_port"])
        except (KeyError, ValueError, TypeError):
            continue
        if not _fresh(ts_s, now_s, max_age_s) or not (0 < src_port <= 65535 and 0 < dst_port <= 65535):
            continue
        src_ip, dst_ip = event.get("src_ip"), event.get("dst_ip")
        source, target = ip_to_service.get(src_ip), ip_to_service.get(dst_ip)
        if not source or not target or source == target:
            continue
        quality = "COMPLETE" if network_sensor_healthy else "PARTIAL_SENSOR"
        network_evidence.append({
            "source": source, "target": target, "src_ip": src_ip,
            "src_port": src_port, "dst_ip": dst_ip, "dst_port": dst_port,
            "protocol": "TCP", "vlan": event.get("vlan_id"),
            "observed_at": ts_s, "sensor_quality": quality,
            "trace_correlation": "UNVERIFIED",
        })
        edge(source, target, "NETWORK_OBSERVED", "OBSERVED", quality)

    return {
        "nodes": nodes, "edges": list(edge_rows.values()),
        "trace_overlay": trace_overlay, "health_overlay": health_overlay,
        "network_evidence": network_evidence, "divergences": divergences,
        "trace_id": trace_id, "schema": "roc.flow-map.v1",
    }
