# Native OpenTelemetry edge receiver

This package prepares the narrow host boundary used by native Andy tracing before
runtime rollout. It does not enable tracing by itself and does not change
application authority, payloads, HMACs, database state, queues, the Generic
Worker, Tempo, or the independent ROC reconstruction bridge.

## Topology

The runtime components remain in their existing isolated network domains. They
send OTLP/HTTP to one host-bound Apache endpoint. Apache accepts only the two
configured source addresses and only `POST /v1/traces`, then proxies to a
Collector port published on host loopback.

The Collector itself is **not** exposed on the LAN and is not attached to the
application VLANs. It remains on the internal `roc-monitoring` network and also
joins the existing non-internal `roc-edge` bridge solely so Docker can publish
the OTLP receiver to host loopback. This does not create a route to the Andy
application VLANs.

```text
Transport domain ----\
                      > host Apache OTLP edge -> 127.0.0.1:14318 -> Collector -> Tempo
Ingress domain ------/
```

Real addresses are host-local configuration. The repository intentionally uses
RFC 5737 documentation addresses only.

## Files

- `apache-site.conf.template`: closed Apache vhost. Default access is denied;
  the OTLP traces endpoint allows POST only from the two configured component
  addresses.
- `render.py`: strict renderer. Unknown keys, duplicates, missing keys, invalid
  IPv4 values and invalid ports fail closed.
- `native-otel-edge.env.example`: documentation-only example. Never replace it
  with real runtime values in Git.
- `roc-otel-loopback.override.yaml`: publishes Collector OTLP/HTTP only on
  `127.0.0.1`.
- `install.sh`: preflight/install/uninstall helper for Debian Apache. Install
  writes the rendered site, runs `apache2ctl configtest`, restores the previous
  site on failure, and reloads Apache only after a valid config.

## Host-local configuration

Create `/etc/attention-router/native-otel-edge.env` from the example and replace
all documentation addresses with the runtime values discovered and reviewed on
that host. Keep it root-owned and outside Git.

Recommended permissions:

```sh
install -d -m 0750 /etc/attention-router
install -m 0640 native-otel-edge.env /etc/attention-router/native-otel-edge.env
```

The file contains network inventory, not application credentials, but it still
belongs to the private host configuration.

## Collector loopback publication

Apply the override together with the existing ROC compose file. Source the same
host-local env first so the loopback port cannot drift from the Apache render:

```sh
set -a
. /etc/attention-router/native-otel-edge.env
set +a

docker compose \
  -p attention-router-roc-e2e \
  -f /path/to/runtime/compose.yaml \
  -f /path/to/release/ops/provisioning/native-otel-edge/roc-otel-loopback.override.yaml \
  up -d roc-otel-collector
```

Expected invariant after recreation: OTLP/HTTP is reachable on
`127.0.0.1:$NATIVE_OTEL_COLLECTOR_LOOPBACK_PORT`, the Collector remains attached
to `roc-monitoring` for Tempo/bridge traffic, it is additionally attached to
`roc-edge` for loopback publication, and OTLP is not bound directly to a LAN
address. `roc-monitoring` is intentionally internal; publishing a port while the
Collector is attached only to that network does not create a reachable host
listener on the deployed Docker runtime.

This step is observability-only. A short Collector restart may lose telemetry,
but must not affect Andy's functional path. The ROC trace bridge remains
independent and reconnects to the same Collector service on its private Docker
network.

## Apache preflight and install

The host must already have Apache with `proxy`, `proxy_http`, `authz_core`
and `authz_host` loaded. The helper does not silently enable modules.

```sh
sudo NATIVE_OTEL_EDGE_ENV_FILE=/etc/attention-router/native-otel-edge.env \
  ./install.sh check

sudo NATIVE_OTEL_EDGE_ENV_FILE=/etc/attention-router/native-otel-edge.env \
  ./install.sh install
```

The edge is intentionally narrow:

- bind address and source addresses are explicit;
- default location is denied;
- only `POST /v1/traces` is accepted;
- request bodies are bounded to 4 MiB;
- Apache access logs include source, method, path and status, never body content;
- upstream is host loopback only;
- no Collector/Tempo health dependency is added to application health checks.

An unauthorized source must receive denial and must not reach the loopback
Collector. Both authorized runtime sources must preserve their original source
addresses at the host boundary; do not deploy this package behind source NAT
without revisiting the allowlist model.

## Runtime tracing rollout

Only after the edge is installed and its deny/allow behavior is certified should
the merged native tracing code be rolled out.

For both components, the OTLP endpoint is the private host edge:

```text
OTEL_TRACING_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://<host-edge-ip>:<host-edge-port>/v1/traces
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_TRACES_SAMPLER=parentbased_traceidratio
OTEL_TRACES_SAMPLER_ARG=1.0
```

Use `1.0` only for the controlled certification canary. Sampling policy after
certification is a separate operational decision.

The Transport and Internal Ingress must be rolled out independently. A failure
to export is allowed to lose telemetry; it is not allowed to change message
admission, HMAC verification, correlation, retry, idempotency or delivery.

## E2E PASS criteria

A port-open test is not sufficient. PASS requires one controlled canary whose
native traces are queried from Tempo and whose graph demonstrates:

1. a Transport `transport.receive` span;
2. a child `transport.ingress_attempt` span;
3. an Ingress `ingress.accept` span continuing that remote attempt context;
4. a separate canonical `attention.message` root linked to the attempt;
5. the canonical root carrying the Andy `roc.correlation_id`;
6. `roc.synthetic=false` / `roc.trace_source=native`;
7. no message body, phone identifier, secret or raw exception content in exported
   Resources, span attributes, events, status descriptions or propagated state;
8. the ROC reconstructed trace continuing to exist independently.

Rollback is component-local: disable tracing or restore the previous runtime
artifact. Do not mutate queues, messages, correlation IDs or the reconstruction
bridge to undo tracing.
