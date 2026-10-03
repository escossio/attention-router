# Gmail Semantic Bootstrap V0

## Purpose

Gmail Semantic Bootstrap V0 makes the already connected Gmail product a historical knowledge source for Personal Context. It reuses the V2B BootstrapRun / BootstrapBatch lifecycle and the Gmail product authority already persisted by GmailConnectionService.

This is not a second Gmail OAuth system and it is not a second memory system.

## Flow

authenticated owner / Client Session
  -> active GOOGLE/GMAIL ProviderAuthorization
  -> exact gmail.readonly scope
  -> GmailBootstrapApiReader
  -> frozen bounded Gmail message-ID snapshot
  -> GmailBootstrapHistoryAdapter
  -> GMAIL_TEXT BootstrapRun
  -> HistoryBackfillService
  -> conversation archive / governed memory

The existing status, pause, resume and cancel controls are shared with the WhatsApp Semantic Bootstrap path.

## Authority

Bootstrap requires all of the following:

- an authenticated Client Session;
- the active tenant derived by the server;
- an ACTIVE GOOGLE/GMAIL ProviderAuthorization for that same human/tenant;
- exact https://www.googleapis.com/auth/gmail.readonly;
- the existing encrypted refresh-token boundary;
- a matching channel.email binding/account fingerprint;
- the first-owner canary tenant when configured.

gmail.metadata remains valid for continuous metadata-only Gmail ingestion but is insufficient for Semantic Bootstrap body observation. V0 never upgrades scope silently.

## Historical snapshot

V0 freezes at most GMAIL_BOOTSTRAP_SNAPSHOT_LIMIT provider message IDs (default 100). The message IDs themselves are carried in the durable bootstrap cursor with an offset.

This gives restart-safe behavior:

1. the first batch captures the bounded provider ID set;
2. later batches read those exact IDs;
3. new Gmail messages cannot reorder the frozen run;
4. a missing/deleted or invalid source message fails closed.

This is intentionally bounded. Deeper historical discovery is a later increment and must not masquerade as an unbounded cursor in V0.

## Text boundary

The bootstrap reader requests Gmail format=full only on this explicit gmail.readonly path.

It observes:

- stable Gmail message ID;
- Gmail thread ID;
- From / To / Cc / Bcc / Subject headers;
- provider timestamp;
- bounded text/plain MIME body parts.

It does not render HTML. Named/attachment MIME parts are not imported as text. A large text/plain body externalized by Gmail behind an attachment ID may be read only as the body part itself and remains bounded by GMAIL_BOOTSTRAP_MAX_BODY_BYTES.

Attachment bootstrap is out of scope; the existing Artifact Plane remains the attachment authority.

## Archive projection

Each Gmail message preserves:

- source gmail;
- account fingerprint from the governed binding;
- gmail:<threadId> as its conversation thread key;
- sender address and display name;
- inbound/outbound direction derived from the authenticated mailbox address;
- DIRECT/GROUP classification from non-owner participants;
- subject + text body as historical text evidence;
- HISTORICAL_UNKNOWN lineage through the existing backfill boundary.

The Gmail adapter never calls neutral live ingress. Historical bootstrap is knowledge ingestion, not a live communication event.

## Runtime

The common bootstrap worker now chooses an adapter by source_kind:

- WHATSAPP_TEXT -> local read-only WhatsApp history adapter;
- GMAIL_TEXT -> governed Gmail bootstrap source.

All runtime flags remain default-off. Gmail has an additional independent GMAIL_BOOTSTRAP_ENABLED gate.

## Safety invariants

- no Gmail send/modify/mark-read/label authority;
- no hidden scope escalation;
- no execution authority;
- no Outbox side effect;
- no canonical Relationship materialization;
- no provider token in archive/cursor/API responses;
- no cross-tenant or cross-owner authorization reuse;
- source account replacement fails closed;
- continuous Gmail metadata/attachment behavior remains unchanged.
