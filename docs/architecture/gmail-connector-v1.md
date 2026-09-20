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

OAuth, refresh tokens and provider credentials belong to the concrete Gmail
reader/plugin process. They are not Attention Router tenant or execution
authority.

The connector itself receives only:

- trusted tenant ID;
- integration instance ID;
- optional account ID;
- neutral ingress URL;
- opaque neutral-ingress bearer.

## Gmail data observed in the connected provider surface

The connected Gmail surface used to validate this design returns the fields
needed by the connector contract:

- Gmail message ID;
- thread ID;
- From/To/Cc/Bcc;
- subject/snippet/body;
- message timestamp;
- labels;
- attachment summaries including provider attachment ID, filename, MIME type
  and size when available.

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

The connector may inspect body/subject only to derive bounded presence metadata.

It does not serialize e-mail body or subject into the neutral V1 event.

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

Attachment-bearing messages fail closed in V1 with:

`GMAIL_ATTACHMENTS_REQUIRE_ARTIFACT_PLANE`

Reason: `EmailNormalizedAdapter` requires staged attachment receipts with
content SHA-256 and storage references. The Gmail read surface exposes provider
attachment IDs but that is not an Artifact Plane receipt.

V1 therefore does not:

- download attachment bytes;
- invent SHA-256 values;
- invent storage references;
- silently drop attachment identity.

The next attachment-capable increment must stage bytes through the Artifact
Plane first.

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

- caller supplies the Gmail query;
- maximum 1..100 messages per cycle;
- each provider message is read then submitted independently;
- 200 duplicate counts as successful replay;
- the neutral ingress remains the idempotency authority.

The connector does not mutate Gmail state, archive messages, apply labels or
mark them read.

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

After merge, create a concrete Gmail reader process around the available
provider/plugin API, provision one `channel.email` integration binding and
neutral-ingress credential, then run a controlled read-only canary with:

- ingress enabled;
- dispatcher enabled;
- one bounded Gmail query;
- no attachment-bearing message;
- no Gmail mutation;
- no Decision Engine dispatch.

That canary should prove the full live chain before any always-on polling.
