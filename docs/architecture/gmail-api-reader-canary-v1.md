# Gmail API Reader + Canary V1

Status: implementation candidate

Related:
- #83 Multi-channel expansion
- Gmail Connector V1
- Neutral Integration Ingress V1
- Neutral Integration Dispatch V1

## Purpose

Provide one concrete, read-only Gmail REST reader and a bounded command-line
canary for the first live e-mail integration proof.

Flow:

`Gmail API -> GmailApiReader -> GmailInboundConnector -> neutral ingress`

The reader does not bypass any previously established boundary.

## Gmail API surface

The reader uses only read methods:

- `users.messages.list`
- `users.messages.get(format=full)`

It does not call:

- `messages.modify`;
- `messages.send`;
- `drafts.*`;
- `labels.*`;
- `attachments.get`.

The intended Google OAuth scope is:

`https://www.googleapis.com/auth/gmail.readonly`

OAuth issuance, refresh-token storage and token rotation remain outside the
Attention Router business-authority plane.

## Token boundary

`GmailApiReader` depends on a narrow `GmailAccessTokenProvider`.

V1 provides `StaticGmailAccessTokenProvider` only for controlled canary use.

The access token:

- is injected at runtime;
- is never persisted by Attention Router;
- is never logged;
- is never sent to the neutral ingress;
- grants Gmail read access only according to the provider-side OAuth scope.

The neutral ingress uses an entirely separate bearer credential bound to one
Attention Router integration installation.

Provider authorization and Attention Router tenant authority therefore remain
independent.

## Provider request shape

`search_message_ids()` issues one bounded `messages.list` request with:

- caller-supplied Gmail query;
- `maxResults <= 100`;
- `includeSpamTrash=false`.

The canary CLI narrows this further to at most 5 messages and defaults to one.

`read_message()` issues:

`messages.get(..., format=full)`

This provides:

- message/thread IDs;
- top-level RFC mail headers;
- Gmail `internalDate`;
- MIME structure;
- attachment IDs/sizes without downloading attachment bytes.

## Timestamp rule

Primary timestamp:

`internalDate`

Fallback:

RFC Date header.

All timestamps are converted to UTC before entering the connector.

## Content minimization

The reader does not pass Gmail snippet or message-body text into the connector.

MIME body structure is inspected only to compute a boolean presence marker.

The connector therefore receives:

- sender identity;
- recipient count inputs;
- message/thread identity;
- subject presence;
- body presence;
- attachment summaries;
- timestamp.

Subject/body content still does not enter Integration Contract V1.

## Attachment boundary

The reader may detect attachment metadata from the MIME tree but never invokes
`attachments.get`.

The existing Gmail Connector V1 then fails closed before neutral ingress when
any attachment is present:

`GMAIL_ATTACHMENTS_REQUIRE_ARTIFACT_PLANE`

No attachment bytes or provider attachment IDs are silently discarded.

## Canary CLI

Script:

`scripts/gmail_integration_canary.py`

Required runtime environment:

- `GMAIL_ACCESS_TOKEN`
- `GMAIL_CONNECTOR_TENANT_ID`
- `GMAIL_CONNECTOR_INSTANCE_ID`
- optional `GMAIL_CONNECTOR_ACCOUNT_ID`
- `ATTENTION_ROUTER_INTEGRATION_INGRESS_URL`
- `ATTENTION_ROUTER_INTEGRATION_BEARER`

Default query:

`in:inbox newer_than:1d -in:spam -in:trash -has:attachment`

Default bound:

1 message.

Hard canary maximum:

5 messages.

The script outputs only aggregate counts:

- selected;
- accepted;
- duplicates.

It never prints:

- Gmail message IDs;
- sender/recipient addresses;
- subjects;
- body/snippet;
- provider responses;
- Gmail access token;
- neutral ingress bearer.

## Error semantics

Reader/provider failures are collapsed into bounded connector errors:

- `GMAIL_API_UNAUTHENTICATED`
- `GMAIL_API_UNAVAILABLE`
- `GMAIL_API_REJECTED`
- `GMAIL_API_RESPONSE_INVALID`

Provider response bodies and credentials are never surfaced in those errors.

## Hard boundary

This increment does not:

- create Google OAuth clients;
- persist refresh tokens;
- deploy or schedule the canary;
- enable neutral ingress or dispatch flags;
- mutate Gmail;
- download attachments;
- send e-mail;
- infer owner authority from a mailbox;
- enqueue the Decision Engine.

## Proof

Tests cover:

- exact read-only Gmail REST request shape;
- bounded Gmail search;
- full-message parsing;
- `internalDate` precedence and Date fallback;
- quoted recipient parsing;
- MIME attachment discovery without `attachments.get`;
- structural body-presence detection with no snippet/body transfer;
- bounded provider error mapping without secret leakage;
- malformed provider-response rejection;
- blank access-token rejection.

## Live canary prerequisite

Before the first end-to-end live canary:

1. deploy a build containing the already-merged neutral ingress/dispatch stack
   and this reader/connector;
2. provision one `channel.email` integration binding and neutral-ingress
   credential;
3. provision a Gmail `gmail.readonly` access token outside Attention Router;
4. explicitly enable ingress and dispatch for the canary environment;
5. run exactly one bounded query excluding attachments;
6. verify `integration_inbox -> CanonicalEvent -> TimelineEvent`;
7. disable the canary path again if always-on polling is not yet approved.

No such deployment or enablement is part of this PR.
