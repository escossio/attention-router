# Channel Sync dedicated service

Public, reproducible package for the [Channel Sync Deployment V1 contract](../../../docs/architecture/channel-sync-deployment-v1.md).
It prepares a future deployment; it does not install IPAM, allocate addresses,
configure MikroTik or start any live container.

## Files

- `channel-sync.compose.yaml`: dedicated outbound-only service using an injected
  certified image, local CLI config check and exactly one external network.
- `channel-sync.env.example`: default-off runtime settings and commented private
  placeholders. It intentionally cannot configure an enabled deployment as-is.

## Prerequisites and private inputs

IPAM is not installed yet. Deployment waits for separately authorized IPAM
installation, one-time inventory bootstrap, reservation of the dedicated trust
zone, physical routing/firewall work and host network creation. Do not reuse the
worker/API zone and do not allocate a sequential candidate in Compose.

Supply these Compose interpolation variables from the operator environment or a
separate protected Compose env file, outside Git:

| Variable | Value supplied privately |
| --- | --- |
| `CHANNEL_SYNC_IMAGE` | certified repository image pinned by digest |
| `CHANNEL_SYNC_ENV_FILE` | absolute path to protected runtime env |
| `CHANNEL_SYNC_NETWORK_NAME` | existing dedicated network governed by IPAM |

The runtime env file is distinct from Compose interpolation. Start with a private
copy of `channel-sync.env.example`, readable only by the deployment operator.
Set the PostgreSQL URL and neutral ingress URL using private service identities.
The ingress path must be `/api/v1/ingress/integrations/events`. PostgreSQL uses the
existing `postgresql+psycopg` driver. Keep real values out of shell history, logs
and rendered config output.

Settings requires `INTERNAL_INGRESS_HMAC_SECRET` even though this process starts
no HMAC listener. `ADMIN_AUTH_ENABLED=false` avoids injecting an unused admin
bearer. For enabled Gmail, the existing `CLIENT_SESSION_ENABLED`, Connect, runner
and scheduler dependency chain must be valid. Reuse the authorized OAuth client
and provider encryption key; refresh grants and integration bearers come only
from governed persistence. Set body ingestion explicitly if authorized; keep
`GMAIL_ATTACHMENT_INGESTION_ENABLED=false` (the service rejects true).

## Validation and future rollout

The following commands are for a **future separately authorized rollout**, after
private inputs are supplied. They are not an instruction to deploy during #265.
From this package directory:

```sh
docker compose -p attention-router-channel-sync -f channel-sync.compose.yaml config --quiet
docker compose -p attention-router-channel-sync -f channel-sync.compose.yaml run --rm --no-deps channel-sync python -m attention_router.infrastructure.channel_sync_service --check
```

The check is structural/offline: no DB connection/mutation, OAuth refresh, Gmail
request or ingress POST. It does not prove provider reachability or freshness.
A provider outage does not affect structural health. There is no HTTP health
listener or published port. Compose overrides the image's API healthcheck with
this local CLI check. Exit 2 means invalid configuration, with sanitized output.

After check success, stop the legacy Gmail scheduler before starting Channel
Sync. Preserve the legacy image/configuration for rollback. Under separate live
authorization, start only this service:

```sh
docker compose -p attention-router-channel-sync -f channel-sync.compose.yaml up -d channel-sync
```

All adapters disabled means a clean exit, not an idle daemon. `restart: on-failure`
restarts abnormal exits but does not loop on disabled configuration. SIGTERM
stops scheduling; the active bounded cycle has a 60-second grace period. After
force-stop, existing transaction rollback/replay semantics apply. One CPU,
512 MiB and 64 PIDs are initial containment bounds; validate operational capacity
later. The root filesystem is read-only; there are no host mounts, Docker socket,
extra capabilities, attachment storage or additional networks.

Observe adapter-key cycle counters and process state. A green config check alone
does not prove progress. Logs are bounded by Docker rotation and exclude mail
body, tokens, bearers and raw provider exception strings. Provider failure
mappings remain owned by the existing adapter/runtime.

## Rollback

Stop the service, restore its previous pinned image/private env and optionally
return to the legacy scheduler after ensuring there is only one active scheduler.
Do not delete ProviderAuthorization, bindings or credentials, reseed
`gmail_history_id`, or modify the database for container rollback. Remove only
this service's network attachment without deleting the external network or
renumbering other services. No worker/API/ingress/schema changes are necessary.

Historical acceleration remains with `HistoryBackfillService`; this package
enables no historical ingestion. Artifact Plane and Gmail attachments are out of
V1. The precursor ingress failure incident is frozen as a separate follow-up.
