# Channel Context Dual-Track Invariant V1

## Purpose

Andy treats communication channels as context sources, not merely message transports.

Every supported channel must be designed with two complementary tracks in mind:

1. **Live continuity** — keep Andy current from newly arriving authorized events.
2. **Historical acceleration** — when useful historical data can be obtained safely and without destabilizing the architecture, use it to make Andy useful faster.

This is a cross-channel product invariant. Gmail and WhatsApp are motivating examples, not special cases.

## Track A — live continuity

The live path answers: **how does Andy stay aware of what is happening now?**

A channel implementation should prefer incremental, automatic processing after the user-authorized boundary instead of depending on a manual refresh button. The exact transport can be polling, provider history/cursor APIs, webhooks, local notifications or another bounded mechanism.

The live path must preserve the existing platform guarantees:

- explicit tenant and identity scope;
- source provenance;
- idempotency and replay safety;
- bounded processing;
- fail-closed authority;
- durable progress/checkpoint state where the provider requires it;
- no assumption that transport delivery alone is semantic truth.

A provider limitation can constrain freshness, but it does not change the product goal: newly available information should be able to become context without requiring the user to repeatedly re-import it.

## Track B — historical acceleration

The historical path answers: **can existing data make Andy contextually useful sooner?**

When a channel, provider, device or user-held export exposes useful prior data, the project should evaluate a bounded historical bootstrap/backfill instead of ignoring that source by default.

Possible historical sources include provider APIs, provider exports, user-provided archives, device-local exports and other explicitly authorized datasets.

Historical acceleration is opportunistic but governed:

- implement it when the source is valuable and it can be added without disorganizing or weakening the core architecture;
- keep it separable from the live path so a historical import cannot silently reseed, skip or corrupt live progress;
- make large imports paginated/resumable and idempotent;
- preserve original source/time/provenance whenever available;
- apply the same tenant, identity, policy and authority boundaries as live ingestion;
- treat historical evidence as evidence, not as permission to act.

If the provider cannot expose useful history, or doing so would currently be unsafe or disproportionately disruptive, record that limitation explicitly rather than letting the second track disappear from the design.

## From channel data to context

Neither track means copying an unlimited raw mailbox or chat archive directly into every model prompt.

Channel data may feed bounded derivation layers such as entities, relationships, events, recurring topics, preferences, temporal patterns and other contextual facts. Derived context must retain enough provenance to distinguish:

- observed evidence;
- normalized facts supported by evidence;
- model/user inferences with confidence or uncertainty.

An inference must not silently become a fact merely because it was derived from many messages.

Retention, eligibility, disclosure and memory policy remain separate concerns from channel transport.

## Change-review rule

Any pull request that changes a channel, connector, scheduler, ingress path, context/memory ingestion path or historical import must explicitly answer both questions:

1. **Live continuity:** what happens to newly arriving information after this change?
2. **Historical acceleration:** can existing history be used here, and what happens to that capability after this change?

A valid answer for either track may be "no behavioral change" or "not implemented", but the PR must explain why. Silence is not an architectural decision.

## Channel examples

### Gmail

Live continuity can use the provider's incremental history/cursor mechanism to discover newly added inbox messages. Historical acceleration is a separate bounded import concern; establishing a live baseline must not be confused with intentionally learning from older mail.

### WhatsApp

Live continuity consumes newly admitted messages through the normal channel ingress. Historical acceleration should be evaluated through whatever authorized history/export source is actually available, without coupling that import to the live transport or weakening provenance.

## Non-goals

This invariant does not require every channel to expose identical APIs, retain the same history window, ingest every byte, or implement historical import in the same PR that introduces live support.

It requires the architecture to preserve awareness of both tracks so the project does not accidentally optimize only for present-time messaging and discard valuable historical context that could make Andy substantially more useful.
