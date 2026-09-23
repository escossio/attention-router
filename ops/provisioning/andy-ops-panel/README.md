# Andy Ops Live Supervisor

LAN-only operational panel for supervising the Attention Router engineering control plane without turning the dashboard into another application dependency.

The first version answers two questions quickly:

1. Are AGT / CI01 / CI02 / CI03 actually using CPU, and what job did AGT assign?
2. Is the Chat -> Remote Desktop Commander -> AGT tool channel still producing observable work even when the ChatGPT UI appears stalled?
3. What HTTP traffic is entering the Client API, and what do the Docker services log for the same runtime activity?

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
- local descendant processes of the Desktop Commander server.

No Attention Router production database is queried.
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
- ANDY_OPS_GOACCESS_URL - optional LAN URL for the GoAccess UI;
- ANDY_OPS_DOZZLE_URL - optional LAN URL for the Dozzle UI.

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

Stale log directories without a final summary are shown as `INCOMPLETE`, not as indefinitely running work.

## Security boundary

- LAN-only by design.
- Read-only observers; no controls or execution buttons in V1.
- No provider credentials or GitHub tokens are required by the web server.
- No production database access.
- Command summaries are redacted, but the UI must still be treated as operationally sensitive.
- Public repository files contain no private host addresses or credentials.

## V1 runtime proof

The original V1 was validated on the engineering control plane with live CPU sampling from all four nodes, real hwmon temperature data where available, distributed-CI history, persistent Desktop Commander tool history, a systemd-managed service, health checks and a headless Chrome render test.
