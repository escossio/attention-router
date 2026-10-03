# Personal Context V2B — Semantic Bootstrap Run Contract

Status: implementation candidate  
Tracking: #221  
Depends on: #220 (merged)  
Base: `main@3632dee3b217e120da153154ce16a827746f072c`

## Purpose

V2B turns the existing historical archive/backfill primitive into an explicit,
owner-initiated Personal Context bootstrap lifecycle.

Target:

`authorized source selection -> BootstrapRun -> bounded batches -> archived evidence -> later semantic processing`

V2B does not create a second archive system.

It reuses `HistoryAdapter` and `HistoryBackfillService`.

## Existing primitive reused

The repository already provides:

- `HistoryAdapter.list_chats()`;
- `HistoryAdapter.fetch_messages()`;
- resumable `HistoryBackfillService`;
- dry-run support;
- conversation/thread/message deduplication;
- secret redaction;
- memory ingestion jobs and extraction runs;
- tenant-scoped archive rows.

Historical data with unknown lineage is not silently promoted into organic
memory. V2B preserves that rule.

## BootstrapRun

One run belongs to exactly one tenant and one represented owner.

Required identity:

- tenant;
- canonical owner actor key;
- source kind;
- source account;
- explicit consent reference;
- stable idempotency key.

Provider-specific selection stays bounded inside `source_selection`.

The initial source kind is `WHATSAPP_TEXT`, but the contract is provider
neutral.

States:

- CREATED
- QUEUED
- RUNNING
- PAUSED
- COMPLETED
- CANCELLED
- FAILED

Controls:

- NONE
- PAUSE
- CANCEL

A PAUSE/CANCEL request during a running batch is applied at the next batch
boundary.

## BootstrapBatch

A batch is one bounded attempt to advance a run.

It records:

- run;
- ordinal;
- deterministic idempotency key;
- cursor before;
- cursor after;
- metrics;
- lifecycle state;
- timestamps;
- bounded failure summary.

A completed batch is immutable evidence of work already performed.

Pause/cancel never deletes completed archive/evidence.

## Progress

Progress is factual counters, not a fake percentage.

V2B tracks metrics already produced by `HistoryBackfillService`, including:

- chats discovered;
- messages discovered;
- messages archived;
- messages skipped;
- secret redactions;
- memory candidates;
- memories promoted;
- archive-only candidates;
- blocked candidates;
- low-confidence candidates;
- errors.

Later semantic stages may extend progress with candidate
entity/relation/episode yield.

## Consent lineage

A run cannot exist without a non-empty `consent_ref`.

V2B stores the consent reference and source selection used to create the run.
It does not infer consent from possession of a provider connection.

## Safety

V2B is ingestion only.

It creates zero:

- capability grants;
- ExecutionIntent rows;
- AgentExecutionIntent rows;
- outbound messages;
- provider-side external actions.

Existing memory lineage rules remain authoritative.

## Idempotency

Run identity is deterministic from:

- tenant;
- owner;
- source kind;
- source account;
- consent ref;
- source revision;
- source selection.

The same exact bootstrap request returns the existing run.

Each batch has a deterministic per-run ordinal identity.

## Failure semantics

One failed batch moves the run to FAILED with a bounded error summary.

Already archived evidence remains preserved.

A later retry policy may be added explicitly; V2B does not silently loop on a
failing source.

## Non-goals

V2B does not yet implement:

- Android bootstrap UI;
- cross-source entity fusion (V2C);
- Episode Builder (V2D);
- Candidate Insight consolidation (V2E);
- graph-aware Context Compiler (V2F);
- GNN/embeddings.

## Acceptance

V2B is complete when:

- run/batch lifecycle is durable;
- source consent lineage is explicit;
- historical import reuses `HistoryBackfillService`;
- processing is bounded/resumable/idempotent;
- pause/resume/cancel work at batch boundaries;
- tenant/owner scope is validated;
- no execution/outbound authority is created;
- migration and tests pass public/distributed CI.
