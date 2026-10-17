# MAESTRO standalone receipt service

This package starts only `create_maestro_tls_app()` in a dedicated container. It
receives and commits authenticated transport receipts in the **existing**
PostgreSQL database. It does not investigate, prioritize, resolve, create tickets,
or start the legacy HTTP ingress. The existing HTTP 18101 service is untouched.

## Communication graph and placement

| Source | Destination | Initiator | Transport | Required | Authority | Zone |
| --- | --- | --- | --- | --- | --- | --- |
| Relay | MAESTRO | Relay | HTTPS 18443 | yes | pinned self-signed certificate, Bearer scoped to tenant/binding/audience | existing Relay bridge to operational network, route must be proven |
| MAESTRO | existing PostgreSQL | MAESTRO | PostgreSQL TCP, existing port | yes | existing DB credential and SQL permissions | operational network to DB zone, route must be proven |
| operator | MAESTRO health | operator | HTTPS 18443 | yes at acceptance | validated certificate | private operational network |

MAESTRO has one attachment to the externally provisioned operational network.
It does not route/forward traffic. L2 and L3 identity and a dedicated static IP
are required by the requested independent operational VLAN and IP-SAN certificate.
No additional management or DB network is justified; DB reachability must be
verified through the single attachment before deployment. Reachability never
grants application authority. The Relay remains on its existing bridge; add no
attachment unless a measured routing failure requires a separate reviewed design.

**Address gate:** the VLAN and subnet are candidates until an authoritative
reservation covers VLAN, subnet, gateway and service IP. Reconcile Host Registry,
RouterOS, Linux VLANs, Docker networks and live addresses privately. Absence of
VLAN 220 or ping response is not an allocation. Never put private values in Git.

## Build and private configuration

Use the repository's existing Attention Router image built and labeled from the
published feature-branch SHA. Set `MAESTRO_IMAGE` to that exact SHA-tagged image;
no second PostgreSQL or Attention Router service is built. Compose also requires
`MAESTRO_RUNTIME_ENV_FILE`, `MAESTRO_DATABASE_URL_HOST_FILE`,
`MAESTRO_CERT_HOST_FILE`, `MAESTRO_KEY_HOST_FILE`, `MAESTRO_NETWORK_NAME` and
`MAESTRO_RESERVED_IP`. Supply these only from a protected operator environment.
Copy `runtime.env.example` to a private 0600 file and set the reserved bind IP.
The URL file contains only the existing `postgresql+psycopg` URL, is mounted
read-only and is not exposed in Docker's configured environment. Give the
container UID 10001 read access to the certificate, URL and key, while keeping
the key 0600 and all three files outside Git. The runner rejects an absent DB
URL, disabled admission, non-PostgreSQL URL, absent TLS material, wildcard/public
bind and group/world-readable key. Docker access logs are off; application
responses do not echo body or bearer. Runtime logs are rotated.

For the minimal stage PKI, generate a short-lived self-signed certificate with
`subjectAltName=IP:<reserved-service-IP>` in a private directory, with `openssl
req -x509 -newkey rsa:3072 -sha256 -days 30 -nodes -addext
subjectAltName=IP:<reserved-service-IP>`; protect the private key
0600. Use the same certificate as the Relay's `MAESTRO_CA_FILE` trust anchor,
mounted read-only through `compose.maestro.yaml`. Validate expiry, SAN and key
permissions before cutover. Never disable hostname or chain verification.

## Acceptance order

1. Record a private checkpoint: source SHA/image revision, current health/status,
   rendered Compose hash, Relay env/image, database revision, network/router
   state and private backup locations. Verify the existing PostgreSQL backup is
   restorable before applying migration 0058. If already at 0058, do not repeat.
2. Reserve the network/address authoritatively, back up RouterOS state, and
   provision only the new VLAN/network. Confirm Relay→MAESTRO and MAESTRO→DB
   routing without modifying legacy firewall, gateway or ingress.
3. Start only the MAESTRO container. Check HTTPS `/health/live` with the pinned
   certificate, including a wrong-CA/hostname negative probe.
4. Create a tenant/binding/credential through the existing mechanism: binding
   `CAPABILITY`, name `ops.zabbix_relay`, audience `maestro-relay-ingress`, scope
   `maestro:relay:write`, trusted instance ID, active tenant. For a synthetic
   stage test, use an explicitly synthetic tenant. Never infer real tenant
   authority from the Relay payload. Back up the DB before any real binding or
   migration; do not reset it.
5. Send one synthetic occurrence via the actual Relay with the optional Compose
   overlay. Require 202 `ACCEPTED` after commit, then 200 `DUPLICATE` with the
   same receipt and no extra row. Verify retry/outbox state and restart. Preserve
   the Relay SQLite outbox. Activate existing Zabbix origin only after those
   checks, without replacing Telegram actions or creating fake real alerts.
6. Verify legacy HTTP, Zabbix, PostgreSQL, Relay and Investigator health again.

No historical import is added: live continuity applies to new authorized events
only; historical acceleration is intentionally deferred with no cursor reseed.

## Rollback

Remove the Relay overlay first so new delivery stops while its outbox remains
durable. Stop only `andy-maestro-standalone`; retain private certs, image and
checkpoint. Remove the new Docker network/VLAN only after confirming no
attachments/dependencies remain. Restore the previous Relay env/image if needed.
Do not downgrade migration 0058 when receipts exist; retain DB data and resolve
the schema separately. No legacy container or PostgreSQL restart is part of
rollback.
