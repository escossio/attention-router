# Channel Sync Deployment V1

## Purpose and scope

Issue #265 packages the [Channel Sync Runtime](channel-sync-runtime-v1.md) as a
dedicated outbound-only process/container. Gmail is the first and only compiled
adapter. This is a repository deployment contract, not a live deployment or
provider enablement. The worker, API, ingress, database schema and provider
contracts do not change.

References: [dual-track invariant](channel-context-dual-track-v1.md),
[communication/network placement](container-communication-network-placement-v1.md),
[Gmail Product Runner](gmail-product-runner-v1.md) and
[public provisioning package](../../ops/provisioning/channel-sync/README.md).

## Process, registry and lifecycle

```text
python -m attention_router.infrastructure.channel_sync_service
python -m attention_router.infrastructure.channel_sync_service --check

Channel Sync Service -> explicit registry: google.gmail
                     -> Channel Sync Runtime
                     -> GmailChannelSyncAdapter
                     -> GmailProductRunner.run_incremental()
```

`build_channel_sync_registry(settings)` returns exactly `google.gmail` in V1,
including its enabled state, batch bound and interval. Construction is
deterministic and performs no DB/provider I/O. There is no plugin discovery,
importlib loader or environment-selected Python module. Future adapters require
reviewed source changes to the registry and their configuration boundary.

Existing `GMAIL_CONNECT_ENABLED`, `GMAIL_PRODUCT_RUNNER_ENABLED` and
`GMAIL_PRODUCT_SCHEDULER_ENABLED` retain their dependency chain and safe defaults.
There is no redundant global enable flag. Body ingestion remains separately
opt-in and exact-scope gated. This service refuses attachment ingestion in V1.
The existing Gmail runner owns OAuth, scopes, historyId, MIME/body, locking and
failure mapping; none is reimplemented by the service.

With no enabled adapters the process exits successfully without discovery or
sleeping, matching the legacy scheduler. Compose uses `restart: on-failure`, so
disabled configuration does not create a restart loop. Invalid configuration
exits 2 with `CHANNEL_SYNC_CONFIG_INVALID`, without validation inputs or traceback.
Enabled scheduling requires PostgreSQL via the existing psycopg driver and a
structurally valid neutral ingress events URL. Shared Settings still requires
its existing HMAC setting and the Connect/ClientSession prerequisite; no listener
is created by satisfying these settings.

Each enabled adapter has independent `after_id`, process-local STALE quarantine
and monotonic next-run state. Cycles run serially, bounded by the existing batch,
message and page limits; the next interval begins after each cycle completes.
This is not parallel provider execution. A slow adapter can delay another in a
future multi-adapter version; V1 only has Gmail. Discovery failures are sanitized
and retried after the interval without discarding rotation/quarantine state.

The service passes `SessionLocal` to the existing runtime: discovery is short,
each installation gets a clean session, success commits, failures roll back.
There are no service-layer commits. SIGTERM/SIGINT stop further scheduling and
interrupt the idle wait; the current bounded cycle may complete during the
60-second Compose grace period. Forced termination rolls back the open DB
transaction; independently admitted HTTP events remain replay-safe under the
existing ingress idempotency contract. Rotation/quarantine are process-local;
restarts may retry STALE once, but never reseed its durable cursor.

The public package uses the repository image with injected digest, read-only
root filesystem, dropped capabilities, no-new-privileges, one CPU, 512 MiB memory,
64 PIDs and bounded local Docker logs. These are initial containment limits, not
measured capacity guarantees. Adjust limits only through reviewed private
operational configuration and subsequent workload evidence.

## Communication matrix

| Source | Destination | Initiator | Protocol/Port | Required | Auth/Authority | Network/Zone |
| --- | --- | --- | --- | --- | --- | --- |
| Channel Sync | PostgreSQL | Channel Sync | TCP/5432 | yes | database credential + tenant/provider authorization revalidation | dedicated Channel Sync zone to routed database zone |
| Channel Sync | Neutral Integration Ingress | Channel Sync | internal HTTP, host-configured port | when admitting events | persisted integration bearer; ingress revalidates credential/binding/audience | dedicated Channel Sync zone to routed ingress zone |
| Channel Sync | Google OAuth token endpoint | Channel Sync | HTTPS/443 | active Gmail execution | OAuth client + encrypted persisted refresh grant | dedicated Channel Sync zone to provider egress |
| Channel Sync | Gmail API | Channel Sync | HTTPS/443 | active Gmail execution | short-lived access token + exact persisted scope | dedicated Channel Sync zone to provider egress |

There is **no inbound application connection** to Channel Sync: no API, Android,
worker, Internet or provider webhook initiator; no HTTP server, published ports
or external endpoint. DNS/routing infrastructure is supplied by the host network
policy and is not an additional application listener or network attachment.
Artifact Plane is outside V1 because attachments remain disabled.

DB traffic carries governed state/cursors; ingress carries authorized normalized
events; OAuth carries refresh credentials and short-lived token responses; Gmail
carries bounded provider metadata and optionally plain text. Network reachability
never grants application authority. Existing tenant, identity, credential,
binding, audience and exact-scope revalidation remains mandatory.

## Addressing and security boundary

```text
Container lifecycle: dedicated
Trust zone: dedicated
Reuse worker/API VLAN: no
Dual-homing: no
New VLAN/subnet: host-private; requires IPAM allocation before rollout
L2 identity: required when deployed via dedicated macvlan/VLAN
L3 identity: required in routed VLAN model
Dedicated IP: required in that deployment model
Static IP: NOT yet justified by application contract;
           resolve only after IPAM/firewall operational design
Published ports: none
Inbound application traffic: none
Networks attached: one external, host-provisioned Channel Sync network
```

Dedicated placement provides independent lifecycle, restart, rollback, network
policy, observability and blast radius, and permits future host movement/scaling
without coupling to worker/API placement. Reachability from those existing zones
does not authorize reusing them. No host networking, NET_ADMIN, privileged mode,
Docker socket, artifact attachment or unnecessary host mount is provided.

## IPAM and host-private configuration

IPAM is not installed yet. Installation is a separate operational frontier. Do
not discover/invent an endpoint or allocate by arithmetic sequence. Future order:

1. Install IPAM under separate authorization.
2. Bootstrap existing inventory once, using a separately authorized MikroTik
   export as an initial source; no continuous MikroTik/IPAM sync engine.
3. Make IPAM authoritative and reserve the dedicated zone's VLAN/subnet/gateway/IP.
4. Resolve static-address/firewall policy; keep allocations and credentials private.
5. Authorize and configure physical networking, host VLAN/macvlan and routing.
6. Supply `CHANNEL_SYNC_NETWORK_NAME`, private runtime env and a certified image.

The public Compose declares only an external network. It does not create physical
networks or supply allocation values. Git carries logical roles, variable names
and optionally a non-sensitive reservation reference, never a private inventory.
No MikroTik mutation, IPAM installation or address reservation belongs to #265.

## Health and observability

`--check` loads Settings, validates structural dependencies and constructs the
registry, then exits 0. It does not open a DB connection, mutate DB state, refresh
OAuth, call Gmail or POST to ingress. An unavailable provider cannot fail this
check. It is **configuration health**, not evidence of DB connectivity, successful
sync, cursor freshness or an unblocked loop. Docker process state detects a dead
main process; operations must separately observe cycle logs for progress.

INFO cycle logs include adapter key and aggregate selected, processed,
initialized, accepted, duplicates, cursor_advanced, BUSY, STALE, quarantined,
unavailable and failed counts. Existing runtime failure logs retain stable
Gmail mappings. Provider exception text, response bodies, mail body, refresh and
access tokens, integration bearers and Settings values are never logged by the
service. No metrics listener or telemetry network is added.

## Future rollout and legacy migration

Only after exact-SHA repository checks and a separately authorized deployment:

1. Satisfy IPAM and host-network prerequisites; pin the certified image digest.
2. Prepare protected host-private env from the example; retain existing keys and
   authority. Keep attachments false. Enable Gmail only explicitly, preserving
   all existing dependency flags and limits.
3. Render Compose privately with `config --quiet`; run the offline `--check`.
4. Stop the legacy standalone Gmail scheduler before enabling the new service.
   Existing locks contain overlap, but simultaneous schedulers are not the
   intended cutover design.
5. Start only Channel Sync and prove governed live continuity with separately
   authorized operational evidence. Observe failures/quarantine and cursor progress.
6. Retire the legacy scheduler only after that future proof; removal is deferred.

`python -m attention_router.infrastructure.gmail_scheduler` remains supported and
unchanged. Both entrypoints reuse `GmailChannelSyncAdapter`; this PR prepares its
replacement operationally without performing the cutover.

## Rollback

Stop/disable Channel Sync, restore the previous pinned image/configuration and,
if needed, resume the legacy scheduler only after the new process is stopped.
Preserve ProviderAuthorization, binding, credentials and `gmail_history_id`.
Never reseed a cursor, delete authorization/binding, or mutate DB state merely to
roll back a container. Remove only this container's network attachment; do not
renumber other services or delete the externally managed network.

Service removal needs no change to worker, API, ingress, Gmail contract, schema
or Personal Context Bootstrap.

## Live continuity

Only the deployment boundary changes. Newly authorized Gmail events continue
through the existing incremental runtime and runner. Default flags remain off;
merging does not start polling or change live cursor semantics.

## Historical acceleration

No new historical ingestion is enabled. `HistoryAdapter -> HistoryBackfillService
-> Personal Context Bootstrap` remains the existing independent boundary. Gmail
historical bootstrap is intentionally deferred; no historyId reuse/reseed and no
second historical engine are introduced.

## Live deployment proof — 2026-10-05

The previously frozen `GMAIL_PRODUCT_INGRESS_FAILED` incident is resolved. The
failure was not a VLAN/L2/L3 problem and did not require wider Channel Sync
network attachment.

The live Gmail runtime had been targeting the Internal Ingress listener with the
Neutral Integration Ingress route. The request therefore reached a healthy
application over the network but received HTTP 404 because that route belongs to
`attention_router.web.ingress_app:app`, not
`attention_router.web.internal_ingress_app:app`.

A separate Neutral Integration Ingress deployment restored the intended surface.
The legacy Gmail scheduler then received real HTTP 202 admissions. During the
first Channel Sync cutover, its dedicated host-private env was found to retain
the old ingress target; after correcting that target and force-recreating the
container, Channel Sync completed a live cycle with `processed=1`,
`accepted=2`, `cursor_advanced=1` and `failed=0`. A following cycle also
completed with `failed=0`.

The legacy Gmail scheduler was stopped before the successful Channel Sync run.
ProviderAuthorization, binding, credential, OAuth scope and Gmail cursor
semantics were preserved; no cursor reseed was performed. Integration dispatch
remains disabled, so admitted work stays queued for a separately authorized
frontier.

Exact private addresses and host inventory remain outside Git. IPAM is still
deferred by operator decision; the manually authorized live allocation must be
imported during the future inventory bootstrap before IPAM becomes authoritative
for new assignments.

Full sanitized evidence and rollback/follow-up boundaries are recorded in
[the live cutover checkpoint](../checkpoints/CHANNEL_SYNC_LIVE_CUTOVER_20261005.md).
