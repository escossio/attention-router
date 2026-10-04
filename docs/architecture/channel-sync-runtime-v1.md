# Channel Sync Runtime V1

## Status

This document defines the target contract for issue #263. The V1 implementation
is intentionally a small extraction from the already-certified Gmail scheduler.
It does not claim that every provider is supported.

The cross-channel product invariant is defined by
[channel-context-dual-track-v1.md](channel-context-dual-track-v1.md).

## Purpose

Channel synchronization has two independent tracks:

1. **Live continuity** keeps Andy current from newly available authorized events.
2. **Historical acceleration** uses bounded prior data to make Andy useful faster
   when an authorized historical source exists.

V1 extracts only the common **live runtime orchestration** proven by Gmail.
Historical acceleration reuses the existing provider-neutral
`HistoryAdapter` + `HistoryBackfillService`; it is not reimplemented here.

```text
                         CHANNEL CONTEXT

            +------------------+------------------+
            |                                     |
      LIVE CONTINUITY                    HISTORICAL ACCELERATION
            |                                     |
    Channel Sync Runtime                     HistoryAdapter
            |                                     |
      provider adapter                  HistoryBackfillService
            |                                     |
            +---------------+---------------------+
                            |
                     archive / context
```

The tracks never share or silently translate cursor/checkpoint state.

## Existing boundaries retained

The Gmail implementation remains authoritative for Gmail-specific behavior:

- Google OAuth and refresh;
- exact `gmail.metadata` / `gmail.readonly` authority;
- Gmail `historyId`;
- `users.history.list`;
- MIME/body and attachment bounds;
- Gmail slot locking;
- Gmail stable failure codes;
- neutral ingress and archive semantics.

V1 does **not** generalize `ProviderAuthorizationRow`. Its current GOOGLE/GMAIL
constraints and `gmail_history_id` column remain unchanged. A future provider
persistence design requires a separate migration and review.

## Provider-neutral live contract

The application contract is deliberately narrow.

`ChannelSyncAdapter` exposes:

- a stable non-secret adapter key;
- capabilities describing whether live continuity and historical acceleration
  are available;
- bounded discovery of eligible installation IDs;
- one live synchronization operation for one installation.

The generic runtime owns:

- a short discovery session;
- one clean session/transaction per installation;
- commit only after a successful adapter result;
- rollback on every failed installation;
- BUSY classification without failing the cycle;
- STALE quarantine for the scheduler process lifetime;
- unavailable authorization classification;
- bounded cycle aggregation;
- logging only stable adapter/error identifiers and unexpected exception types.

Provider exceptions and provider response bodies are never logged by the generic
runtime.

## Failure classes

The provider-neutral runtime understands four stable classes:

- `CHANNEL_SYNC_BUSY` — ordinary contention; rollback and retry in a later cycle.
- `CHANNEL_SYNC_STALE` — live checkpoint cannot safely continue; rollback and
  quarantine the installation for the current process.
- `CHANNEL_SYNC_UNAVAILABLE` — installation/authority is not currently eligible.
- `CHANNEL_SYNC_PROVIDER_FAILURE` — provider-specific governed failure; rollback
  and count as failed while preserving only a stable code.

Unexpected exceptions are rolled back and logged only by exception type.

Adapters translate their provider-specific errors into these classes. Gmail
continues to own the meaning of its existing `GMAIL_*` codes.

## Gmail V1 adapter

The Gmail adapter is a delegation layer, not a rewrite.

Discovery keeps the existing ACTIVE GOOGLE/GMAIL predicate and rotating bounded
page semantics. Live execution delegates to
`GmailProductRunner.run_incremental()`.

The adapter converts the existing `GmailHistoryResult` into the neutral live
result and maps:

- `GmailProductHistoryBusy` -> `CHANNEL_SYNC_BUSY`;
- `GmailProductHistoryStale` -> `CHANNEL_SYNC_STALE`;
- `GmailProductAuthorizationUnavailable` -> `CHANNEL_SYNC_UNAVAILABLE`;
- other governed `GmailProductRunnerError` values ->
  `CHANNEL_SYNC_PROVIDER_FAILURE` while retaining the stable Gmail code.

No Gmail network request, cursor rule, body rule, scope rule or transaction
ordering changes in this extraction.

## Historical acceleration boundary

Historical acceleration already has a provider-neutral engine:

- `HistoryAdapter`;
- `HistoryBackfillService`;
- resumable cursor;
- dry-run support;
- bounded per-chat and total budgets;
- archive deduplication.

`Personal Context Bootstrap V2B` already consumes that engine. Its current
supported source set is intentionally narrower and currently includes
`WHATSAPP_TEXT`.

Adding Gmail historical acceleration therefore means adding an authorized Gmail
history source/adapter to the existing bootstrap path in a future increment. It
does **not** mean reusing Gmail live `historyId`, reseeding the live cursor or
building a second backfill engine.

## Security and authority invariants

- The adapter never chooses tenant or represented human authority from provider
  payloads.
- Provider-specific authorization is revalidated at execution time.
- Live cursor state and historical bootstrap state are independent.
- Historical evidence does not grant execution authority.
- No adapter may broaden provider scopes.
- No live sync operation performs provider mutation unless a future contract
  explicitly introduces and authorizes it.
- Secrets, raw provider error bodies and raw channel content are excluded from
  scheduler logs.
- Neutral ingress, archive lineage, memory eligibility and disclosure policy
  remain separate boundaries.

## Deployment topology

V1 defines code boundaries only. It does not enable a new live service.

The intended future deployment is a dedicated channel-sync process/container,
separate from the core Attention Router worker, because provider I/O must not
delay decision, timer, outbox or personal-context work.

A deployment PR must separately define:

- process/container lifecycle;
- configuration injection;
- health/readiness;
- restart policy;
- resource bounds;
- observability;
- rollback;
- exact enabled adapters.

Until that PR is certified and explicitly deployed, the existing Gmail scheduler
entry point remains the only concrete live scheduler.

## Change review

### Live continuity

V1 preserves Gmail live behavior while moving transaction/cycle orchestration
behind a provider-neutral contract. New provider adapters can later reuse the
same runtime without coupling provider I/O to the core worker.

### Historical acceleration

No new historical ingestion is enabled. V1 explicitly reuses
`HistoryBackfillService` and keeps historical state separate from live progress.
Gmail historical bootstrap remains future work.

## Non-goals

- no provider-generic database migration;
- no WhatsApp/Calendar/Drive adapter in V1;
- no mailbox backfill;
- no dynamic plugin loader;
- no Android UX change;
- no runtime flag enablement;
- no live deployment.
