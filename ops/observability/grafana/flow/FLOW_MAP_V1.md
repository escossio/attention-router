# Flow Map / Live Message Path V1 — read-only contract

This slice introduces a pure projection into Grafana Infinity Node Graph `nodes`
and `edges`. It is deliberately **not deployed** and performs no IO, runtime
mutation, packet capture or Zabbix/Tempo configuration changes.

## Authority and trust boundaries

- **Zabbix ROC Docker discovery** is the inventory and health evidence. Each
  container is resolved by the stable Compose `project/service/replica` entity key,
  not an ephemeral container ID or network address.
- Each Zabbix sample must retain the item history `clock`, not only JSON
  `state`/`health`. Missing, out-of-window, or future samples resolve to
  `UNKNOWN` regardless of the last value. A `running` Docker state without
  a health-check result is also not equivalent to `healthy`.
- Legacy `roc.entity.*` alarms are secondary divergence evidence for services
  migrated into containers. They cannot override fresh, canonical Docker
  discovery health. A separate reconciliation of their collectors is needed.
- **Tempo**'s existing `/view` projection contributes native trace stage data,
  intra-trace parent/child edges and *verified* cross-trace SPAN_LINK edges.
  Other stages remain stages, never fabricated container nodes. `NOT_REACHED`
  is not automatically an error.
- **TCP Brain** contributes metadata-only L3/L4 observations. A witnessed
  5-tuple does not prove it belongs to a selected application trace. V1 marks
  this correlation `UNVERIFIED`, and honors sensor health. Packet absence
  during capture drops is never a proven missing network hop.

## Input and output contract

`flow_map.build_flow_map(...)` accepts explicit:

- `service_entities`: OTel service name -> *observed* Zabbix Docker entity key;
- `container_samples`: {clock, value} Zabbix JSON history rows;
- `attachment_samples`: {clock, value} Zabbix attachment history rows;
- `expected_edges`: operator-declared pairs, clearly tagged `EXPECTED_ONLY`;
- `trace_view`: projected Tempo `/view` JSON for `latest` or a selected ID;
- `network_events`: metadata-only TCP Brain observations, optional;
- `now_s`, `max_age_s`, `network_sensor_healthy`.

It returns `nodes`, `edges`, `health_overlay`, `trace_overlay`,
`network_evidence`, and `divergences`. Only allowlisted metadata is projected.
No packet/message bodies, peer phone numbers, arbitrary OTel attribute values,
environment variables, or credentials are exported.

Nodes have `id`, `title`, `subTitle`, `mainStat`, `secondaryStat` and
source/evidence details. Edges have `id`, `source`, `target`,
`mainStat`, `secondaryStat` and `relationship`.

In Infinity 4.x, select the dedicated `Nodes - Node Graph` and
`Edges - Node Graph` formats, using JSON root selectors `nodes`/`edges`,
respectively. A Grafana runtime panel should only be installed *after* a
read-only source adapter exposes valid, fresh Zabbix history and the chosen
trace; do not provision an empty or synthetic live panel.

## Limits / next integration gate

- The current Reader remains Tempo-only and has no Andy/Zabbix database
  privileges. Do not grant it credentials or mount the Docker socket.
- The adapter between canonical ROC Zabbix and the projection has not yet
  been deployed. It must use an existing, scoped read path; preserve Zabbix
  observation timestamps; fail closed on stale polling; and avoid new
  privileged connections.
- Network evidence requires a bounded event source, explicit sensor state and
  sufficiently fresh container attachments. Current TCP Brain anomaly events
  alone do not establish every successful hop.
- The first map must remain limited to the *observed* Transport, Ingress and
  Worker entities; explicit service-to-entity mapping is injected, not guessed.
- Visualization animation is a distinct future feature; Node Graph V1 should
  not pretend to animate packet-level causality.
- The native Tempo Service Graph metrics-generator is not a prerequisite.

Checks: `python3 -m pytest -q tests/test_grafana_flow_reader.py
tests/test_grafana_flow_map.py`. Real runtime read-only smoke is separate
from deterministic tests. No real inventory snapshot is committed.
