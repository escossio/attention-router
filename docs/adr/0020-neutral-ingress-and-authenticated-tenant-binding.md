# ADR 0020: Neutral Ingress and Authenticated Tenant Binding

## Status

Proposed HTTP design, 2026-09-11. An
[isolated offline binding proof](../integration-tenant-binding-proof-v0.md) now
implements the credential/tenant decision. No endpoint, production credential
registry, migration or HTTP client is implemented by this increment.

## Context

The two-adapter proof, portable V1 wire schema and standalone Python/TypeScript
contract SDKs are complete in main at
`1c0d02d9788d7951be222e7c436efeaae845d353` (PRs #39, #41 and #42).
All seven required checks passed for that commit.

The [current internal ingress](../explicit-tenant-runtime-v0.md) authenticates
`timestamp + "." + body` with a shared HMAC secret and checks that the explicit
tenant exists and is active. It does not bind that secret to a particular tenant
or integration installation. A valid schema or signature therefore does not
establish permission to choose the tenant.

The neutral event bridge creates an `EventEnvelope`; it does not provide durable
HTTP admission or a lossless route into the running conversation pipeline.
Building a client now would freeze an unproven authentication and retry contract.

## Decision

Define the [neutral ingress V0 profile](../integration-transport-v0.md) before a
network client. Its first operation admits one V1 `inbound_event` over HTTPS at
`POST /api/v1/ingress/integrations/events`, on the ingress surface.

Each machine credential resolves to one server-owned binding: deployment,
tenant, integration kind/name, instance and optional account. The server obtains
the authoritative tenant from this binding and rejects a payload that disagrees.
Bindings have no wildcard tenants or accounts. A connector serving several
tenants uses separate credentials and bindings.

Use opaque Bearer credentials through `Authorization`, with TLS, server-side
scope/lifecycle checks, hash-only credential storage and rotation. The
[Bearer HTTP scheme](https://www.rfc-editor.org/rfc/rfc6750.html#section-1) supports
tokens issued outside an OAuth flow. This decision does not introduce an OAuth
authorization server. Possession of a token permits its holder to act within the
binding; protecting and revoking it remains essential.

Admission is durable and idempotent within the stable binding, independently of
credential rotation. The receiver stores the request identity and recoverable
pending work atomically before acknowledging it. Retries reuse the original body
bytes and key. Conflicting bytes or an event replayed under a different key fail
closed. This is admission deduplication, not an exactly-once execution guarantee.

The acknowledgement describes receipt, never policy approval, provider execution
or delivery evidence. This follows the distinction between admission and finished
processing in [HTTP 202](https://www.rfc-editor.org/rfc/rfc9110.html#section-15.3.3).
The existing owner, actor, artifact and execution boundaries still apply.

## Consequences

The current WhatsApp HMAC ingress remains the current implementation; adopting
this design does not harden or migrate it automatically. SDK validation also
continues to establish shape only. The proposed server requires its own negative
authorization tests and PostgreSQL concurrency/durability proof before a client.

V0 starts with inbound admission. Delivery, invocation, artifact upload and
provider results need separate direction-specific authorization and evidence
contracts. Accepting all five wire families at one generic endpoint would erase
those distinctions.

Byte-preserving retries avoid introducing cross-language JSON canonicalization
into the existing schema. They require a connector to persist the serialized
request before sending it; equivalent JSON serialized differently can conflict.
This tradeoff must be explicit in the later client API.

## Alternatives considered

| Alternative | Decision |
| --- | --- |
| Reuse the global HMAC secret plus a body tenant | Rejected for the new surface: authenticates the tenant assertion without limiting which tenant the principal may assert. |
| HMAC per binding | Could establish the same tenant boundary, but also requires a signing profile, key selection and replay rules. Defer unless message-level signatures become a requirement. |
| HTTP Message Signatures | A future option for proof of possession. [RFC 9421](https://www.rfc-editor.org/rfc/rfc9421.html#section-7.2) requires careful coverage and replay handling; it still needs a tenant binding. |
| OAuth/JWT or mutual TLS | Possible later credential mechanisms behind the same binding decision; provisioning and lifecycle integration are outside this increment. |
| Tenant from provider JSON, a query parameter or `X-Tenant-ID` | Rejected as authority. Public webhooks must first authenticate the provider and resolve its account/resource through trusted server data. |

The offline credential/binding decision proof covers the authorization subset of
the acceptance matrix. Authoritative registry storage and durable server admission
are next. A production dispatcher, WhatsApp migration and outbound client remain
separate boundaries.
