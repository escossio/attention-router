# Andy Ops Live Supervisor

LAN-only operational panel for supervising the Attention Router engineering control plane without turning the dashboard into another application dependency.

The first version answers two questions quickly:

1. Are AGT / CI01 / CI02 / CI03 actually using CPU, and what job did AGT assign?
2. Is the Chat -> Remote Desktop Commander -> AGT tool channel still producing observable work even when the ChatGPT UI appears stalled?
3. What HTTP traffic is entering the Client API, and what do the Docker services log for the same runtime activity?
4. Do the WhatsApp browser, transport, owner-authority and backend states agree with each other?
5. Where is a recent message in the governed inbound-to-outbound pipeline?

## Scope

The panel is intentionally small. It is not a general observability stack, not a production APM, and not an authority source for Attention Router business logic.

### Compute view

Four large cards show:

- AGT control plane;
- CI01 worker;
- CI02 worker;
- CI03 KVM Virtualized Worker;
- aggregate CPU and per-core block meters inspired by `btop`;
- CPU/package temperature when Linux exposes a trustworthy hwmon sensor;
- `RUNNING`, `IDLE`, or `OFFLINE`;
- current assigned CI job and elapsed time;
- recent distributed PostgreSQL dispatches and worker results.

RAM is intentionally absent from V1 because it is not an operational bottleneck for this lab.
### Chat / Console view

The panel does not claim to know whether the ChatGPT graphical interface itself is frozen.

Instead it observes the concrete execution channel used by ChatGPT to operate AGT:

`Chat -> Remote Desktop Commander -> AGT`

The installed Remote Desktop Commander keeps a bounded JSONL tool-call history. V1 reads that history read-only and shows:

- plugin process online/offline state;
- most recent tool call;
- tool-call duration and result;
- recent console child processes;
- summarized recent tool calls.

Arguments are summarized and obvious token/secret/password/bearer patterns are redacted before data reaches the browser. The panel must remain LAN-only because command summaries can still contain operational context.

### Agent / Transport and Message Trace views

The AGENT / TRANSPORT view is a read-only state-reconciliation surface. It compares the real WhatsApp page exposed through CDP, the transport service status, whatsapp-web.js connectivity, owner command authority, observer freshness and Attention Router readiness. Divergence between those independent sources is surfaced explicitly instead of being collapsed into a single synthetic health bit.

The same view can show a bounded recent MESSAGE TRACE. The tracer reads only identifiers, timestamps, statuses and control-plane metadata needed to reconstruct stages such as INBOUND, INGRESS, CONTROL, QUEUE, DECISION, EXECUTION, OUTBOX and OUTBOUND. It does not read or expose message bodies, attachment bytes or provider credentials.

Message tracing is optional and disabled when `ANDY_OPS_TRACE_DB_CONTAINER` is blank. When enabled, the panel invokes a fixed read-only SQL query through `docker exec ... psql` against the configured local database container. The HTTP UI has no write endpoint and cannot supply arbitrary SQL or container names.

### HTTP / GoAccess and Containers / Dozzle views

Two optional lazy-loaded tabs embed the existing LAN-only observability UIs without proxying or duplicating their data:

- HTTP / GOACCESS embeds the Apache / Client API traffic view;
- CONTAINERS / DOZZLE embeds the Docker log viewer.

The panel does not gain Docker socket access and does not parse application logs itself. URLs are host configuration supplied by environment and are never hardcoded in the public package. The server CSP admits only the configured frame origins.

## Architecture

`browser -> stdlib Python HTTP server on AGT`

The server samples:

- local `/proc/stat` and `/sys/class/hwmon`;
- the same sources over SSH aliases for CI01/CI02/CI03;
- `/var/log/andy-ci/*-postgres-distributed/summary.json`;
- the Remote Desktop Commander JSONL tool history;
- local descendant processes of the Desktop Commander server;
- optional WhatsApp transport/browser/observer probes configured by environment;
- optional bounded message-stage metadata from the configured local PostgreSQL container.

The message tracer performs a fixed read-only query only when explicitly configured. It does not fetch message bodies or artifact bytes.
## Configuration

The public package contains no LAN addresses. Host-specific values stay outside Git.

Environment variables:

- `ANDY_OPS_LISTEN_ADDRESS` - defaults to `127.0.0.1`;
- `ANDY_OPS_LISTEN_PORT` - defaults to `18121`;
- `ANDY_OPS_POLL_SECONDS` - defaults to `1.5`;
- `ANDY_OPS_SSH_TIMEOUT` - defaults to `1.2`;
- `ANDY_OPS_CI01_HOST`, `ANDY_OPS_CI02_HOST`, `ANDY_OPS_CI03_HOST` - SSH aliases;
- `ANDY_OPS_CI_LOG_ROOT` - defaults to `/var/log/andy-ci`;
- `ANDY_OPS_TOOL_HISTORY` - defaults to the current user's Desktop Commander JSONL history path.
- `ANDY_OPS_WHATSAPP_TRANSPORT_STATUS_URL`, `ANDY_OPS_WHATSAPP_BROWSER_DEBUG_URL`, `ANDY_OPS_ATTENTION_API_READY_URL` - optional private runtime probes for AGENT / TRANSPORT.
- `ANDY_OPS_WHATSAPP_TRANSPORT_UNIT`, `ANDY_OPS_WHATSAPP_BROWSER_UNIT`, `ANDY_OPS_WHATSAPP_OBSERVER_UNIT` - optional systemd unit overrides for transport reconciliation.
- `ANDY_OPS_TRACE_DB_CONTAINER` - optional local PostgreSQL container name; blank disables message tracing.
- `ANDY_OPS_TRACE_DB_USER`, `ANDY_OPS_TRACE_DB_NAME` - database identity used by the fixed read-only trace query.
- `ANDY_OPS_TRACE_LIMIT` - recent traces requested, clamped to 1..50.
- ANDY_OPS_GOACCESS_URL - optional LAN URL for the GoAccess UI;
- ANDY_OPS_DOZZLE_URL - optional LAN URL for the Dozzle UI.
- `ANDY_OPS_NETWORK_OSI_URL` - optional URL of the authenticated Grafana ROC
  `roc-network-osi` dashboard. The NETWORK / OSI tab loads its iframe only when
  selected. The browser uses its existing Grafana session; no Zabbix API call,
  password or token is added to the panel. The CSP admits the configured origin.
  See [NETWORK / OSI provisioning](../../observability/network-osi/README.md).

Copy `andy-ops-panel.env.example` to `/etc/default/andy-ops-panel` and set the private LAN bind there.

## Installation

Run as an operator with permission to install systemd units:

`bash ops/provisioning/andy-ops-panel/install.sh`

The package installs to `/srv/andy-ops-panel` by default and enables `andy-ops-panel.service`.

Health endpoint:

`/healthz`

Read-only JSON API:

`/api/status`

The browser polls every 1.5 seconds.
## Temperature behavior

The collector prioritizes CPU-oriented hwmon sources such as `coretemp`, `k10temp`, package/Tctl/Tdie labels and then ACPI sensors.

GPU temperatures are ignored for the CPU card.

If a guest does not expose thermal telemetry, the UI displays `N/A`. It never fabricates a host temperature.

## Distributed CI semantics

AGT is the control plane and CI01/CI02/CI03 are workers.

Live process inspection recognizes `andy-ci-distributed`, `andy-ci-reprofile`, `andy-ci-run` and PostgreSQL pytest shards. Completed distributed jobs are loaded from the control-plane summaries.

A dispatch is shown as `RUNNING` only when a live AGT/worker process reports that exact SHA. Directory age alone never implies active work. Early scheduler failures persist a terminal `summary.json` and therefore appear as `FAIL` with a failure class; a log directory without both a terminal summary and a live process is shown as `INCOMPLETE`.

## Security boundary

- LAN-only by design.
- Read-only observers; no controls or execution buttons in V1.
- No provider credentials or GitHub tokens are required by the web server.
- Optional production-database access is read-only, bounded and disabled unless a local DB container is explicitly configured.
- Message tracing uses a fixed SQL statement and exposes stage metadata only; the browser cannot submit SQL or Docker commands.
- Command summaries are redacted, but the UI must still be treated as operationally sensitive.
- Public repository files contain no private host addresses or credentials.

## V1 runtime proof

The original V1 was validated on the engineering control plane with live CPU sampling from all four nodes, real hwmon temperature data where available, distributed-CI history, persistent Desktop Commander tool history, a systemd-managed service, health checks and a headless Chrome render test.
