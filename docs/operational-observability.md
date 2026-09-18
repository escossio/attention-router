# Lightweight operational observability

Attention Router uses a lightweight, out-of-band observability layer for local/runtime inspection. These tools are operator aids; they are not part of the request path and do not change application authority, proxy behavior, or database semantics.

## Tooling

### GoAccess

GoAccess provides a live HTTP view of the public Client API traffic from the Apache access log.

Current AGT installation baseline:

- GoAccess 1.9.3
- local web UI port: `7891`
- realtime WebSocket port: `7890`
- bound to the AGT LAN interface only
- no bind on `0.0.0.0`
- no public Internet exposure

The Client API access log is intentionally compact rather than Apache Combined format:

```text
timestamp route method status duration_us client_ip
```

Example shape:

```text
2026-09-17T22:33:16-0300 verify-and-continue POST 200 964831 192.0.2.10
```

A local feed normalizes that record into a GoAccess-compatible request line while preserving the response duration in microseconds. This makes challenge, verify, and verify-and-continue activity visible with method, status, source and latency without changing the Apache request path.

GoAccess is observational only. It does not proxy, terminate TLS, authenticate users, or mutate requests.

### Dozzle

Dozzle provides a browser UI for Docker container logs.

Current AGT installation baseline:

- image: `amir20/dozzle:latest`
- image digest observed at installation: `sha256:7c4fb7f8124f5dea15ade881535cc19cc9a03e66639fd28154ff43a93b78944b`
- local UI port: `18080`
- bound to the AGT LAN interface only
- Docker socket mounted read-only
- no public Internet exposure

The `latest` tag is mutable and the digest above records the image actually pulled for this operational installation. Future reproducible deployment should pin an immutable digest rather than assuming `latest` is stable.

Dozzle is an operator-facing log viewer, not an application dependency.

## Security boundary

Both UIs are intended for the trusted local network only.

Operational requirements:

- never bind the observability UIs to `0.0.0.0`;
- do not publish their ports through router/NAT rules;
- do not place them behind the public Client API hostname;
- do not treat access to these UIs as an application authentication mechanism;
- avoid logging credentials, Google ID tokens, continuation grants, private conversations, session material, or secret values.

Dozzle has visibility into container logs and therefore remains an administrative surface even when the Docker socket is mounted read-only.

## Relationship to application observability

This lightweight layer currently gives two complementary views:

```text
Android / external client
        |
        v
Public Client API / Apache
        |
        +--> compact access log --> GoAccess
        |
        v
Attention Router services
        |
        +--> Docker logs --> Dozzle
```

This is intentionally simpler than a centralized logging/metrics stack. If correlation, retention, metrics, traces or multi-host search become necessary, that should be introduced as a separate observability frontier rather than by placing these tools in the application request path.

## Current proven use

The V0.3A live proof produced a traceable HTTP sequence in the Client API access log:

```text
challenge             POST 201
verify-and-continue   POST 200
```

The dashboards make this kind of runtime evidence visible without requiring repeated ad-hoc searches through Apache and container logs.
