# Gmail Connector V1

Status: implementation candidate

Related:
- #83 Multi-channel expansion
- Neutral Integration Ingress V1
- Neutral Integration Dispatch V1
- EmailNormalizedAdapter
- Integration Contract V1

## Purpose

Provide the first concrete live-provider connector shape for the multi-channel
architecture without teaching Attention Router core about Gmail.

Flow:

`Gmail reader -> GmailInboundConnector -> EmailNormalizedAdapter -> neutral HTTP ingress`

After ingress, existing boundaries own the event:

`integration_inbox -> canonical dispatcher -> CanonicalEvent -> Timeline`

## Provider boundary

The connector depends on a narrow `GmailReader` protocol:

- search message IDs using a Gmail query;
- read one message into a bounded `GmailMessage`.

OAuth, refresh tokens and provider credentials are not tenant or execution
authority. In the subscriber product path, refresh tokens remain inside the
encrypted ProviderAuthorization envelope. A trusted server-side runner may
decrypt them in memory to obtain a transient access token for GmailApiReader.
The reader itself never persists provider credentials.

The connector itself receives only:

- trusted tenant ID;
- integration instance ID;
- optional account ID;
- neutral ingress URL;
- opaque neutral-ingress bearer.

## Gmail data observed in the product metadata profile

The subscriber product requests only
`https://www.googleapis.com/auth/gmail.metadata`. The product reader therefore
uses the Gmail metadata surface and observes only the fields needed by the
reference event:

- Gmail message ID;
- thread ID;
- selected message headers (From/To/Cc/Bcc/Subject/Date);
- internal message timestamp;
- mailbox label membership used for bounded INBOX selection.

It does not request message bodies or attachment bytes. The product reader
marks body and attachment observation as false rather than inventing absence.
The connector intentionally does not depend on Gmail labels for identity or
authority.

## Timestamp normalization

The provider surface can expose both offset-aware timestamps and UTC wall-clock
timestamps without an explicit offset.

The connector:

- preserves explicit offsets and converts to UTC;
- treats a provider timestamp with no offset as UTC.

This rule is connector-local and covered by tests.

## Content minimization

The provider-neutral EmailNormalizedAdapter currently emits a reference event.

The product metadata reader does not observe message body content. Subject is
used only to derive bounded presence metadata and is never serialized into the
neutral V1 event.

The V1 event carries:

- Gmail message ID as external event identity;
- Gmail thread ID as external thread identity;
- sender mailbox as external actor identity;
- `EMAIL_MESSAGE_REFERENCE`;
- opaque `gmail:<message_id>` message reference;
- sanitized presence/count metadata.

The neutral dispatcher subsequently keeps only the durable
`integration_inbox_id` in canonical payload metadata.

## Attachments

The generic connector still fails closed with
`GMAIL_ATTACHMENTS_REQUIRE_ARTIFACT_PLANE` when a reader actually supplies
attachment summaries without staged Artifact Plane receipts.

The subscriber product reader uses `gmail.metadata`, which does not observe
attachment content and must not claim that a message has zero attachments. It
therefore emits `attachments_observed=false` and omits `attachment_count`.
No attachment bytes, provider attachment IDs, invented SHA-256 values or
invented storage references cross this boundary.

The next attachment-capable increment must explicitly request an appropriate
scope and stage bytes through the Artifact Plane first.

## Neutral ingress client

The connector includes a small stdlib HTTP client.

It sends only:

- POST;
- `Authorization: Bearer <opaque integration credential>`;
- `Content-Type: application/json; charset=utf-8`;
- serialized Integration Contract V1 event.

Accepted outcomes:

- 202 / accepted;
- 200 / duplicate.

Bounded server errors are surfaced as connector errors. Network failures are
reported as `INGRESS_UNAVAILABLE`.

The connector never sends tenant or idempotency authority in alternate headers
or query parameters.

## Polling semantics

`GmailInboundConnector.poll()` is intentionally simple and bounded:

- the product metadata profile selects the INBOX label without using Gmail `q`;
- `GmailApiReader` rejects a non-empty `q` locally because `gmail.metadata`
  does not permit that list parameter;
- maximum 1..100 messages per cycle;
- each provider message is read as `format=metadata` and submitted independently;
- 200 duplicate counts as successful replay;
- the neutral ingress remains the idempotency authority.

The connector does not mutate Gmail state, archive messages, apply labels or
mark them read. A durable history cursor is a later always-on polling increment;
the first product canary remains bounded and replay-safe through neutral-ingress
idempotency.

## Hard boundary

V1 does not:

- provision Google OAuth;
- persist Google tokens in Attention Router;
- enable integration ingress/dispatch flags;
- deploy a daemon;
- send or reply to e-mail;
- read/download attachment bytes;
- grant owner authority from Gmail identity;
- create capability or execution authority;
- bypass neutral ingress.

## Proof

Unit tests cover:

- connected-provider timestamp shapes;
- sender mailbox parsing;
- body/subject minimization;
- exact EmailNormalizedAdapter V1 projection;
- Authorization/content-type neutral HTTP request;
- accepted and duplicate responses;
- bounded 401/409/503 rejection propagation;
- attachment fail-closed behavior;
- bounded poll size.

No production Gmail mailbox is modified by these tests.

## Next safe step

After merge, create a product-governed polling runner around the existing ACTIVE
ProviderAuthorization created by Connect Gmail. The runner must reuse its
existing `channel.email` binding and encrypted neutral-ingress bearer; no manual
provisioning or copied token is part of subscriber UX.

Run one controlled read-only canary with:

- refresh-token exchange performed server-side in memory;
- ingress enabled;
- dispatcher enabled only for the neutral canonical path under test;
- one bounded INBOX metadata poll;
- no Gmail mutation;
- no body/attachment read;
- no Decision Engine dispatch.

That canary should prove the full live chain before any always-on polling or
durable Gmail history cursor.
