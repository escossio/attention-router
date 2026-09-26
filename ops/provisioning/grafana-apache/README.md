# Grafana Apache authentication and Live boundary

This vhost keeps Apache Basic authentication in front of Grafana's own session
authentication. It does not enable anonymous access, change either credential,
or alter Grafana, its datasources, dashboards, Tempo, Collector or application
services. TLS termination is provided by the existing external edge.

The specific `/api/live/ws` WebSocket mapping must precede the generic HTTP
mapping. `RequestHeader unset Authorization` runs in the normal **late** fixup
phase: Apache validates Basic authentication first, then removes that credential
before proxying. Do not add `early`, which would remove it before authentication.
Grafana login and session cookies continue through the proxy. External API
clients that previously relied on forwarding Authorization must use a separately
designed authentication boundary; this operator UI vhost does not forward it.

Requires Apache modules `proxy`, `proxy_http`, `proxy_wstunnel`, `headers`,
`auth_basic`, `authn_file` and `authz_user`. Check loaded modules; do not restart
services or enable unrelated modules.

## Render and deploy

Read the effective vhost and preserve a backup first. Use host-local values;
never put real credentials or password hashes in this package.

```sh
python3 ops/provisioning/grafana-apache/render.py \
  --server-name grafana.example.invalid \
  --auth-user-file /etc/apache2/grafana.htpasswd \
  --upstream-port 18083 > /tmp/grafana.candidate.conf
```

Compare the candidate with the existing site before installation. Preserve the
existing enabled-site symlink, permissions and other vhosts. After installing the
reviewed candidate at the site's resolved path:

```sh
apache2ctl configtest
systemctl reload apache2
```

Reload only after `Syntax OK`. On configtest failure restore the backup before
any reload. Rollback restores that same backup, runs configtest, then reloads.
No container restart is necessary. Rollback reintroduces the known Live/auth
faults and should be reserved for an actual regression.

## Regression checks

The short isolated regression uses the actual rendered vhost, a synthetic
upstream and synthetic credentials. It starts a separate Apache process on an
ephemeral loopback port and stops it without reloading the system service.
Run as root on Debian with `apache2` and `apache2-utils` already installed:

```sh
python3 -B ops/provisioning/grafana-apache/check_proxy.py
```

1. No Apache credential: HTTP 401 with a Basic challenge.
2. Valid Apache credential without Grafana session: protected Grafana API remains
   HTTP 401, but no Grafana password-auth attempt is caused by the proxy.
3. Valid Apache credential and Grafana session: dashboard/API HTTP 200.
4. A valid RFC 6455 handshake, including Origin and session cookie, returns 101,
   `Upgrade: websocket`, `Connection: Upgrade` and a valid Sec-WebSocket-Accept
   through both the origin proxy and external edge.
5. Observe the operational dashboard through repeated automatic refreshes;
   capture page errors, datasource query statuses and Live handshakes without
   capturing Authorization, passwords or cookies.
6. Compare Grafana health, datasource health, Tempo readiness/retrieval and
   accepted-span counters. Verify protected container IDs/start times and
   configuration digests are unchanged.

The DOM `insertBefore` exception is not established as a consequence of these
proxy faults. If it persists, isolate the panel/plugin and browser rendering
conditions; do not change unrelated infrastructure.

References: [Apache WebSocket proxying](https://httpd.apache.org/docs/2.4/mod/mod_proxy_wstunnel.html)
and [RequestHeader execution phase](https://httpd.apache.org/docs/2.4/mod/mod_headers.html#requestheader).
