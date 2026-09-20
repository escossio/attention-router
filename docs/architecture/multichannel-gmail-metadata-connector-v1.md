# Multi-channel Gmail Metadata Connector V1

Status: implementation candidate

Related:
- #83 Multi-channel expansion
- Neutral Integration Ingress V1 / PR #132
- Neutral Integration Dispatch V1 / PR #133
- `EmailNormalizedAdapter`
- Artifact Plane V0

## Purpose

Introduce the first live non-WhatsApp provider connector without making Gmail a
core architecture dependency.

Target:

`Gmail API -> Gmail metadata connector -> EmailNormalizedAdapter -> V1 inbound_event -> neutral ingress -> integration_inbox -> canonical dispatcher`

The provider-specific code lives under `attention_router/connectors`. The
core continues to see only the versioned neutral integration contract.

## Initial capability

V1 is metadata-only.

The connector requests a Gmail OAuth access token provisioned outside Attention
Router and is designed for:

`https://www.googleapis.com/auth/gmail.metadata`

This stage does not request message-body or send-mail authority.

For each newly added INBOX message the connector retrieves only message
metadata needed to create the neutral event:

- Gmail message ID;
- Gmail thread ID;
- internalDate;
- From header.

It does not request or forward:

- message body;
- subject text;
- recipients;
- attachment bytes;
- labels as business authority;
- OAuth token details.

The current EmailNormalizedAdapter therefore emits:

- `EMAIL_MESSAGE_REFERENCE`;
- sender external identity;
- thread reference;
- occurred/received time;
- deterministic correlation/idempotency;
- zero inline artifacts.

Provider-private references remain inside the authenticated V1 contract and the
durable integration inbox. The canonical dispatcher keeps only the durable
`integration_inbox_id` reference.

## Incremental synchronization

The connector follows Gmail's history-based synchronization model.

### Bootstrap

If there is no local cursor:

1. call Gmail `users.getProfile`;
2. store the current mailbox `historyId`;
3. store only SHA-256 of the mailbox e-mail identity;
4. ingest no historical messages.

This deliberately avoids silently importing an entire mailbox on first start.

### Poll

For later cycles:

1. re-read profile and verify mailbox fingerprint;
2. call `users.history.list` from the stored `historyId`;
3. request only `messageAdded` history for `INBOX`;
4. deduplicate message IDs across history records/pages;
5. fetch each message with `format=metadata`, requesting only `From`;
6. normalize with `EmailNormalizedAdapter`;
7. serialize the V1 contract deterministically;
8. submit to the neutral HTTPS ingress;
9. accept only ingress `accepted` or `duplicate`;
10. advance the cursor only after every selected message succeeds.

A partial successful admission followed by a later failure does not advance the
cursor. The next poll resends deterministic byte-identical contracts and relies
on the ingress idempotency boundary.

## Expired history cursor

Gmail may return HTTP 404 when `startHistoryId` is outside the available
history window.

V1 treats that as:

`GMAIL_HISTORY_CURSOR_EXPIRED`

and does not mutate the cursor.

It does not silently skip to the current history ID and does not automatically
perform an unbounded historical import.

Operator-controlled resynchronization is required.

## Backlog bounds

The connector is bounded by:

- `GMAIL_MESSAGE_LIMIT_PER_POLL` (default 100, maximum 500);
- `GMAIL_HISTORY_PAGE_LIMIT` (default 20, maximum 100).

If either bound is exceeded, the poll fails before any Gmail message is sent to
the neutral ingress and the cursor remains unchanged.

This avoids silently acknowledging an incomplete history window.

## Credential boundary

Two independent credentials exist:

1. Gmail OAuth access token — provider read authority only;
2. Attention Router integration Bearer — authenticated admission identity only.

Neither grants business/execution authority.

Preferred deployment uses file-backed secrets:

- `GMAIL_ACCESS_TOKEN_FILE`;
- `ATTENTION_ROUTER_INTEGRATION_CREDENTIAL_FILE`.

The Gmail access-token file is re-read for every provider request, allowing a
separate credential agent to rotate short-lived OAuth tokens.

OAuth authorization/refresh mechanics are explicitly outside this connector
slice.

## Cursor state

The cursor state file contains only:

- schema version;
- SHA-256 mailbox fingerprint;
- Gmail historyId.

No access token, e-mail address, message ID, body or provider payload is stored.

The file is written atomically and mode 0600. Loading a group/world-readable
cursor fails closed.

## Transport security

The neutral integration client requires:

- HTTPS endpoint;
- no URL userinfo;
- no query/fragment authority;
- redirects disabled;
- exact Bearer header;
- deterministic serialized request bytes;
- response correlation ID equal to the submitted event.

3xx and non-200/202 responses are failures.

## Mailbox identity protection

Every poll compares the current profile e-mail fingerprint to the bootstrap
fingerprint.

If an OAuth credential is accidentally swapped to another Gmail mailbox:

`GMAIL_MAILBOX_IDENTITY_CHANGED`

is raised before history retrieval or integration admission.

No cross-mailbox cursor reuse occurs.

## Deterministic retry

The normalized event uses Gmail `internalDate` as both source event time and
connector received time.

That value is provider-stable, so a retry of the same Gmail message produces
the same V1 bytes.

The ingress can therefore return `duplicate` instead of an idempotency
conflict.

## Deployment controls

The connector itself does not enable Attention Router runtime flags.

A live end-to-end proof additionally requires an operator to provision:

- one active `channel.email` integration binding;
- one integration ingress credential for that binding;
- `INTEGRATION_INGRESS_ENABLED=true`;
- `INTEGRATION_DISPATCH_ENABLED=true`;
- trusted HTTPS termination for the integration ingress.

These are rollout operations, not part of this code change.

## Systemd template

Repository templates:

- `ops/systemd/attention-gmail-connector.service`;
- `ops/systemd/gmail-connector.env.example`.

The service keeps connector state under:

`/var/lib/attention-router/gmail-connector`

and expects secrets outside Git under `/etc/attention-router`.

## Attachments

V1 intentionally does not fetch Gmail attachment data.

The existing `EmailNormalizedAdapter` + Artifact Plane contract remains the
required boundary for attachments. A later e-mail slice must stage attachment
bytes into the Artifact Plane before emitting artifact receipt IDs; it must not
place binary content or arbitrary Gmail attachment URLs into the neutral event.

## Proof

Tests cover:

- first-run bootstrap without historical ingestion;
- incremental `messageAdded` history sync;
- INBOX filter;
- metadata-only message fetch;
- EmailNormalizedAdapter output;
- deterministic V1 request bytes;
- accepted and duplicate neutral ingress responses;
- ingress failure leaves cursor unchanged;
- Gmail history 404 leaves cursor unchanged;
- mailbox identity change fails before ingestion;
- backlog overflow fails before any message admission;
- cursor state mode 0600 and unsafe-permission rejection;
- HTTPS-only neutral ingress client;
- redirect/error/mismatched-correlation rejection.

## Next gate

After repository gates pass, the remaining distinction is operational:

**implemented live-capable connector** vs. **live proof on an authorized Gmail
mailbox**.

The live proof should bootstrap the connector, then receive one deliberately
sent test e-mail and verify:

Gmail
-> neutral ingress receipt
-> integration inbox PROCESSED
-> CanonicalEvent(channel.email)
-> Timeline MESSAGE_RECEIVED

without exposing body text or provider tokens.
