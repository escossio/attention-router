# Andy Ops Live Supervisor

LAN-only operational panel for supervising the Attention Router engineering control plane without turning the dashboard into another application dependency.

The first version answers two questions quickly:

1. Are AGT / CI01 / CI02 / CI03 actually using CPU, and what job did AGT assign?
2. Is the Chat -> Remote Desktop Commander -> AGT tool channel still producing observable work even when the ChatGPT UI appears stalled?
3. What HTTP traffic is entering the Client API, and what do the Docker services log for the same runtime activity?
4. Are Browser / WhatsApp transport / owner authority / observer states coherent, and where is the latest message in the inbound-to-outbound pipeline?

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
- local descendant processes of the Desktop Commander server;
- optional, default-off, bounded read-only WhatsApp transport/browser/observer probes;
- optional, default-off, bounded metadata-only message trace projection from the configured runtime database container.

Message tracing is default-off and must be explicitly enabled on the host. When enabled, it issues bounded SELECT-only queries and deliberately excludes message bodies, outbox destinations, full correlation ids and full inbound-event ids from the browser payload. The database container is host configuration; no runtime container name or credential is committed.

The default configuration does not query the Attention Router database. Message tracing is separately gated, bounded, SELECT-only and LAN-only.
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
- `ANDY_OPS_TRANSPORT_OBSERVABILITY_ENABLED` - defaults to `false`; enables the read-only AGENT / TRANSPORT probes;
- `ANDY_OPS_WHATSAPP_RUNTIME_BACKEND` - defaults to `container`; reads the Transport status endpoint and the observer's bounded `status.json`. The panel needs no Docker socket or CDP exposure. `legacy_systemd` exists only for the pre-cutover deployment and must be removed after successful soak;
- `ANDY_OPS_WHATSAPP_OBSERVER_STATUS_FILE` - local path to the observer's mounted status projection, without message bodies;
- `ANDY_OPS_MESSAGE_TRACING_ENABLED` - defaults to `false`; enables bounded message-lifecycle tracing;
- `ANDY_OPS_TRACE_DB_CONTAINER` - host-private container name required when message tracing is enabled.
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
- No database mutation. The optional message tracer performs bounded SELECT-only metadata projection when explicitly configured; message bodies, destinations and full event/correlation identifiers are excluded from its browser payload.
- Command summaries are redacted, but the UI must still be treated as operationally sensitive.
- Public repository files contain no private host addresses or credentials.

## V1 runtime proof

The original V1 was validated on the engineering control plane with live CPU sampling from all four nodes, real hwmon temperature data where available, distributed-CI history, persistent Desktop Commander tool history, a systemd-managed service, health checks and a headless Chrome render test.

## WhatsApp container observability

Andy Ops reads the Transport `/status`, the Observer bind-mounted `status.json`, and read-only `docker inspect`/`docker logs` for the three WhatsApp containers. The panel process needs read access to Docker and `/var/lib/attention-router/whatsapp-observer/status.json`. Host CDP and retired systemd units are not operational sensors. Missing page evidence is reported as `OBSERVABILITY_GAP`, never as a measured zero. Docker logs are projected to a fixed event-name allowlist; message bodies and credentials are not returned. The boot/AppArmor orchestration issue remains a separate follow-up.

## Context / Performance

A aba **CONTEXT / PERFORMANCE** conserva os estilos, componentes, navegação e
estados do Live Supervisor. É configurada por
`ANDY_OPS_CONTEXT_PERFORMANCE_SOURCE`, uma origem HTTP loopback com porta
explícita. Os endpoints same-origin da aba fazem somente leitura. O Apache e
seus controles de autenticação continuam na frente do painel. A URL opcional
`ANDY_OPS_CONTEXT_TEMPO_URL` abre o Explore autenticado no Grafana existente.
Sem essa URL, a aba usa a origem de NETWORK / OSI, quando disponível.

- **LIVE** atualiza durante o ensaio, apropria a amostra de COMPUTE do worker já
  presente em `/api/status` e distingue cadência, execução, esperas conhecidas
  e resultados. Memória do host fica como indisponível quando COMPUTE não a
  recolhe. Memória do processo de bancada tem fonte própria e rótulo explícito.
- **TRACE** exibe as etapas, correlação e intervalos observados, e permite abrir
  o trace nativo no Tempo. Tempos inclusivos de etapas aninhadas não são somados.
- **COMPARE** usa Run ID, SHA e fingerprints do experimento. Resultados com
  condições incompatíveis ou metadados parciais são sinalizados.

O operador inicia a bancada descartável no worker e acompanha a aba. A aba
não dispõe de API de execução, reprocessamento, autoridade ou publicação.
Veja o [contrato experimental](../../observability/g7-mgmt-experiment.md).


## TCPBrain status in NETWORK / OSI

The existing NETWORK / OSI view keeps its ROC/Grafana iframe and adds a
read-only TCPBrain section. The same protected MGMT origin serves
`GET /api/tcpbrain/status` (schema `tcpbrain.mgmt.v1`). There is no new
listener, network attachment, IP allocation, raw event subscription, provider
call or write to the data plane. The UI polls every two seconds only while the
tab is active and the document is visible.

The adapter projects a fixed allowlist from the two capture status files and
the flow analyzer status file under the runtime status directory. It reads
bounded regular files, rejects unknown schemas and treats samples older than
five seconds as stale. Missing or invalid metrics are null, not zero. No raw
packet, IP/MAC, payload, last-event text, credentials or service inventory is
returned. Use the existing authenticated management boundary; the server
itself is not a replacement for that boundary.

Capture discards, subscriber delivery failures, flow input connectivity and
observation coverage are independent. Legacy producers are displayed as
unverified; V2 counters and watermarks are interpreted when available, with
epoch and freshness checks. A bounded window is not proof of complete physical
wire observation. Observed forwarding totals are cumulative for a process
epoch; an unobserved expected egress does not establish root cause.

This does not integrate TCPBrain path events into the ROC flow-map or change
the source's NOT_INTEGRATED contract. That integration requires a separate
review. It does not depend on promotion of a new TCPBrain collector build.

Targeted offline validation:
`python -B -m unittest discover -s tests -p 'test_tcpbrain_mgmt*.py'` from
the repository root; `node --check ops/provisioning/andy-ops-panel/tcpbrain-mgmt.js`.
The dedicated CI runs these tests. Before rollout, verify the exact approved
revision, existing authenticated access, same-origin assets, health and
unrelated tabs. Back up the existing server and index, install the new assets
before the new entry points, and restart only MGMT in an authorized window.
Restore exact originals and remove only newly added files on regression.
