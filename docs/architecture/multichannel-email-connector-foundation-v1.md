# Multi-channel E-mail Connector Foundation V1

Status: implementation candidate

Related:
- #83 Multi-channel expansion
- Neutral Integration Ingress V1
- Neutral Integration Dispatch V1
- EmailNormalizedAdapter

## Purpose

Create the provider-side seam for the first live e-mail integration without
putting Gmail, IMAP, OAuth or provider credentials inside Attention Router
business logic.

Flow:

`native provider -> RFC822 Message -> EmailConnector -> EmailNormalizedAdapter -> neutral HTTP ingress`

The already-merged neutral ingress/dispatch path remains authoritative after
that boundary.

## Why provider-neutral RFC822 first

Gmail, Microsoft Graph and IMAP differ in:

- authentication;
- change notification/polling;
- provider message/thread identifiers;
- pagination/history cursors;
- attachment retrieval.

They can all yield an RFC822/MIME message or an equivalent normalized snapshot.

V1 therefore freezes the connector contract before selecting one live provider.
A later Gmail/Graph/IMAP implementation only has to fetch messages and hand
them to this seam.

## Data minimization

The connector extracts only bounded metadata needed by the existing
`EmailNormalizedAdapter`:

- Message-ID;
- thread ID supplied by the provider connector, or Message-ID fallback;
- sender address/display name;
- timezone-aware Date;
- whether subject exists;
- whether a text body exists;
- recipient count;
- opaque message reference.

It does **not** copy:

- message body text;
- subject text;
- provider OAuth token;
- provider refresh token;
- attachment bytes.

The body remains provider-side behind an opaque connector reference.

## Attachments

Artifact Plane transport is not implemented in this slice.

Therefore the connector sends no attachment receipts and does not claim
attachments were staged.

A provider connector that sees attachments must keep them provider-side until a
separate governed Artifact Plane upload/receipt boundary exists.

## Neutral ingress client

`IntegrationIngressClient` is a thin connector-side HTTP client.

It:

- sends the exact Integration Contract V1 JSON;
- uses the provisioned neutral ingress Bearer;
- accepts only 202 accepted or 200 duplicate;
- validates transport version, status, receipt fields and exact correlation ID;
- bounds response size;
- never includes response body or bearer material in raised errors.

It does not provision credentials or choose a tenant.

## Authority

Tenant, integration instance and account are connector configuration.

They are not read from message headers/body.

The Attention Router neutral ingress still authenticates the Bearer against the
server-owned integration binding before admission.

E-mail sender identity remains external evidence only. Canonical actor identity
is resolved later by the installation-scoped actor binding introduced by the
neutral dispatcher.

## Provider credentials

Provider OAuth/API credentials are explicitly outside this contract.

A future live provider implementation owns:

- consent/OAuth;
- provider refresh token;
- provider scopes;
- history cursor/subscription;
- native fetch retries.

Those credentials are not Attention Router execution authority and must not be
forwarded to the neutral ingress.

## Proof

Tests cover:

- RFC822 metadata extraction;
- body content is not copied into the snapshot/contract;
- subject content is not copied;
- EmailNormalizedAdapter emits channel.email V1 contract;
- connector sends Bearer-authenticated JSON to the neutral ingress client;
- duplicate receipt is accepted idempotently;
- wrong correlation response fails closed;
- HTTP error bodies and bearer secrets are not exposed in connector errors;
- missing Message-ID, Date or sender fails closed;
- deterministic message reference is stable and opaque.

## Runtime state

No production connector daemon.

No provider OAuth.

No Gmail/Graph/IMAP dependency.

No feature flag enabled.

No deploy.

## Next safe slice

Choose and implement one native provider adapter behind this seam.

For a Gmail-first path:

`Gmail history/watch or bounded polling -> fetch RFC822 -> EmailConnector`

For an IMAP-first path:

`IMAP UID cursor -> fetch RFC822 -> EmailConnector`

The provider choice must not change Attention Router core contracts.
