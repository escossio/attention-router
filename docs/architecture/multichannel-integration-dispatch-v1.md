# Multi-channel Integration Dispatch V1

Status: implementation candidate

Related:
- #83 Multi-channel expansion
- Neutral Integration Ingress V1 / PR #132
- Integration Contract V1
- Artifact Plane V0
- Personal Context V1

## Purpose

Consume already-authenticated, durable `integration_inbox` work and bridge it
into the provider-neutral canonical event/timeline plane without forcing
non-WhatsApp sources through the legacy message/decision DTO.

Target:

`integration_inbox(PENDING) -> CanonicalEventRow -> TimelineEventRow -> PROCESSED`

This stage is internal-only. It does not invoke the Decision Engine, a provider,
an outbox, a capability or an external effect.

## Why canonical-first

The legacy `NormalizedInboundEvent` requires message content and a resolved
actor. The neutral Integration Contract intentionally supports reference-only
events such as e-mail:

- external actor may be present but not canonically resolved;
- body/message content may remain behind an opaque provider reference;
- thread and artifact references may exist without inline content.

V1 therefore dispatches neutral events first into Canonical Event + Timeline,
which already support optional actor identity and opaque payload references.

No provider-specific core conditional is introduced.

## Durable lossless reference

The canonical event does not copy provider-native IDs or message references
wholesale.

It stores:

`payload_ref.integration_inbox_id`

The authenticated inbox row remains the durable exact-byte source of truth for
the complete V1 contract.

This preserves losslessness by reference while keeping canonical metadata small
and bounded.

Canonical metadata contains only structural information such as:

- integration binding ID;
- integration kind/name/instance;
- whether an account is present;
- whether an external actor/thread is present;
- whether actor resolution succeeded;
- thread kind;
- artifact count;
- contract schema version.

External actor IDs, provider message references and thread IDs are not copied
into canonical metadata.

## Actor correlation boundary

Actor identity is never invented.

A neutral integration event may resolve an actor only through an explicit,
active `ActorBindingRow` under this installation-specific namespace:

`integration:<integration_binding_id>`

The exact external actor ID must match that binding.

This means:

- the same e-mail address in two integration installations does not silently
  collapse to one canonical actor;
- a missing actor binding yields `actor_id = null`;
- external actor IDs longer than the existing ActorBinding storage contract are
  left unresolved, never truncated;
- future cross-channel identity correlation remains a separate governed layer.

## Runtime authority revalidation

Admission authenticates the connector at receipt time, but dispatch rechecks
current server authority before starting new work.

The dispatcher locks in this order:

`tenant -> integration binding`

and requires:

- tenant still ACTIVE;
- binding still ACTIVE;
- binding still belongs to the admitted tenant;
- binding still contains `integration:inbound_event:write`.

This lock order matches the admission/provisioning boundary and serializes
dispatch against binding/tenant disable.

Credential revocation does not retroactively cancel already-admitted work.
Binding/tenant/scope disable blocks pending work from starting.

## Inbox lifecycle

Migration `0044_integration_dispatch_v1` extends the durable inbox:

- `PENDING`
- `PROCESSED`
- `BLOCKED`

PROCESSED requires:

- one canonical_event_id;
- processed_at;
- no dispatch_reason.

BLOCKED requires:

- no canonical event;
- processed_at;
- a bounded dispatch_reason.

The existing PostgreSQL immutability trigger remains authoritative. It permits
only the one-way dispatch transition from PENDING to PROCESSED/BLOCKED and
continues to reject mutation/deletion of receipt identity or raw bytes.

A populated dispatch lifecycle cannot be downgraded without explicit data
export.

## Atomicity and concurrency

The dispatcher uses `FOR UPDATE SKIP LOCKED` on PENDING inbox rows.

Canonical event creation, timeline creation and inbox transition occur in the
same database transaction.

Therefore:

- crash/rollback before commit leaves the inbox PENDING;
- a committed PROCESSED inbox has its canonical event;
- no external effect occurs inside the transaction;
- concurrent workers do not dispatch the same receipt twice.

Canonical event identity is deterministic from the inbox receipt, providing an
additional recovery/idempotency guard.

## Canonical projection

For channel integrations:

`origin = EXTERNAL_INBOUND`

For capability/provider events:

`origin = PROVIDER_EVENT`

The exact contract event_type and payload_type are preserved on CanonicalEvent.

Timeline projection is bounded:

- message -> MESSAGE_RECEIVED
- call -> CALL_EVENT_RECEIVED
- other -> INTEGRATION_EVENT_RECEIVED

The original exact event type remains on CanonicalEvent and in the durable V1
contract.

## Runtime gate

Adds:

- `INTEGRATION_DISPATCH_ENABLED=false`
- `INTEGRATION_DISPATCH_BATCH_SIZE=20`

Dispatch remains OFF by default.

Neutral HTTP admission and neutral dispatch can therefore be rolled out
independently.

## Hard boundary

This stage does not:

- enqueue Agent decisions;
- synthesize message text;
- dereference provider URLs;
- read attachment bytes;
- create Artifact Plane receipts from unregistered IDs;
- grant owner/from_me authority;
- create capability grants;
- create ExecutionIntentRow;
- send replies or notifications;
- enable the neutral ingress or dispatcher in production.

## Proof

PostgreSQL tests cover:

- authenticated e-mail reference -> one CanonicalEvent + one TimelineEvent;
- exact raw contract remains reachable through integration_inbox_id;
- provider-private actor/message/thread IDs are not copied into canonical
  metadata;
- explicit installation-scoped actor binding resolves the canonical actor;
- actor binding under another integration namespace never cross-resolves;
- binding disable after admission blocks dispatch;
- inbound scope removal after admission blocks dispatch;
- tenant disable after admission blocks dispatch;
- concurrent binding disable that commits first is observed by dispatch;
- repeated dispatcher cycles create no duplicate canonical/timeline rows;
- database rejects invalid/destructive inbox mutations;
- processed dispatch state blocks downgrade without export;
- worker dispatch gate remains OFF by default and forwards bounded batch size.

## Next safe stage

After this PR is integrated, the next #83 slice is the first live e-mail
connector/provider:

native e-mail provider
-> EmailNormalizedAdapter
-> neutral HTTP ingress
-> durable inbox
-> canonical dispatcher

Provider OAuth/credentials remain connector-side and never become Attention
Router business authority.
