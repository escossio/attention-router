# Andy Worker lifecycle V3: pinned OFF/ON transition contract

Status: candidate. This document describes a versioned controller and synthetic
validation. It does not authorize installation, restart, migration or activation.
Private Compose files, boot settings and manifests stay outside Git.

## Purpose and scope

`scripts/operations/andy_live_runtime_v3.py` extends the V2 six-input Worker
parity guard to support a scoped First Contact OFF/ON render and a pinned
source-to-target image transition. It preserves the visitor default and guard,
ClientSession, group scope, execution switches, image provenance, complete
Compose render and Worker/DB restart and health policy. The controller only
addresses the declared Worker service. It does not change policy, tenant
binding, outbox state, Transport, Browser, Observer or PostgreSQL schema.

The six ordered roles are `base`, `otel`, `otel_e2e`, `execution`, `groups` and
`visitor_recovery`. The V3 manifest pins each absolute path and SHA-256, the
canonical rendered Compose JSON hash, an immutable `sha256:` image reference
and matching image ID, source revision, critical environment value hashes,
project, workdir, service and container identities. `first_contact_mode` must
be `off` or `on`. ON requires `FIRST_CONTACT_ENABLED=true` and explicit tenant
and source-account scope matching separate boot pins
`ATTENTION_LIVE_FIRST_CONTACT_TENANT_ID` and
`ATTENTION_LIVE_FIRST_CONTACT_SOURCE_ACCOUNT`; OFF requires false. The image OCI revision must match
the pinned source SHA. A service with no healthy Docker healthcheck fails the
post-start gate.

## Transition, reboot and reversal

The target manifest is root-only, separately pinned by
`ATTENTION_LIVE_WORKER_MANIFEST_SHA256`. A transition also requires a pinned
previous manifest and `ATTENTION_LIVE_WORKER_MANIFEST_SHA256_PREVIOUS`. The
previous schema may be V2 (OFF baseline) or V3. Both manifests must agree on
project, workdir, Worker/DB containers and service. The old six Compose files
must remain immutable at their original paths while the old container declares
them; place target files at separate versioned paths. Editing overlays in place
invalidates the source proof and must stop the transition.

`--check` verifies the target already running. `--transition-check` validates
the target inputs and the currently running source without writes. The default
reconcile path verifies the current source or already-installed target, starts
and waits for the declared DB, revalidates all pinned inputs, and runs only
`docker compose up -d --no-deps --pull never --no-build <service>`. It then
waits for explicit healthy status and verifies complete runtime parity.
Reconciliation with already-installed target supports startup after a reboot;
synthetic tests cover stopped containers. A drift or ambiguous effect fails
closed. An operator must assess a failed external recreate before retrying.

Directed rollback uses an inverse pinned transition: previous = new render,
target = saved OFF render and saved image ID. Keep schema 0060 and all
presentation, ACK, ambiguous-effect, outbox and ledger records; never replay or
backfill to make rollback appear successful. The controller does not perform an
automatic second external effect after an ambiguous result. Installation of a
root-owned package, boot default and unit is a separate approved operation.

## Validation boundary

Unit fixtures use synthetic six-file renders, source/target image pins,
permission and drift faults, explicit health, stopped-container reboot and
ON-to-OFF rollback ordering. A real `docker compose config` preflight was
performed against synthetic files. Runtime startup/health and component
compatibility require an isolated stack; no synthetic result proves production
health or WhatsApp delivery. The exact commit and CI checks must be recorded
before any operational promotion.

Live continuity remains on the existing admitted-event path. Historical
acceleration is deliberately unchanged: no cursor reset, historical import or
semantic-memory requirement is introduced. No container or network is added
by this controller; it invokes the existing local Docker/Compose control path.
L2 identity, L3 identity, dedicated IP and static IP are not required. Network
reachability is unchanged and never grants application authority.
