# ROC E2E flow dashboard

Canonical dashboard: `../dashboards/roc-e2e-operational.json` (same UID). Native Tempo trace panels use `roc-tempo`; summary tables use `roc-flow` with Grafana Labs Infinity **4.0.0**. The read-only reader queries only Tempo, exposes no host port and has no Andy/database credentials. It bounds searches to 12 native traces / 24 hours and shares a 10-second cache across panels.

Deployment: install Infinity through Grafana's plugin API, then `docker compose -f compose.yaml up -d --build` on the existing monitoring network. Copy `../provisioning/datasources/roc-flow.yaml` into the existing provisioning directory and reload with `POST /api/admin/provisioning/datasources/reload`. Back up the current dashboard, copy the canonical JSON into its existing provisioned path, and call `POST /api/admin/provisioning/dashboards/reload`. No existing service restart is required.

Use `trace_id=latest` or paste a canonical trace ID; the dashboard has a certified-canary link. Native trace views and Explore links keep inbound and canonical parent trees separate. `SPAN_LINK` is metadata, not a fabricated parent edge. Missing stages are `NOT_REACHED`; an existing span with no explicit status is `UNSET`. Outcomes describe exported evidence, not a live database status. Durations of overlapping spans must not be summed. Service Graph requires a metrics backend not supplied here.

`build_dashboard.py EXISTING_JSON OUTPUT_JSON` preserves the existing Zabbix/legacy panels and can be rerun on the generated definition. The checked-in JSON is deployable without regeneration. Focused tests: `pytest -q tests/test_grafana_flow_reader.py`.

Rollback: restore the dashboard backup and reload dashboard provisioning, remove only the `roc-flow` datasource provisioning file and datasource, then stop this reader with `docker compose -f compose.yaml down`. Keep the existing Tempo/Zabbix datasource definitions and all functional OTel services unchanged.
