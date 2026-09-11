# Neutral Integration Transport and Tenant Binding V0

**Status: proposed HTTP design; isolated offline binding proof implemented.**
The [binding proof](integration-tenant-binding-proof-v0.md) covers a subset of the
acceptance matrix, without implementing a receiver, database registry or client.
This specifies the next boundary
after the [contract SDK extraction](integration-sdks-v0.md), under
[ADR 0020](adr/0020-neutral-ingress-and-authenticated-tenant-binding.md).
V0 names this design increment; the proposed HTTP profile uses version `1`.
Neither status implies a published service or a network-capable SDK.

## Baseline and scope

Verified baseline: main `1c0d02d9788d7951be222e7c436efeaae845d353`.

| Current implementation | Proposed boundary |
| --- | --- |
| [V1 JSON contract](integration-contract-v0.md), two reference adapters and installable validation SDKs | Reuse the same wire schema unchanged. |
| [Internal ingress](../attention_router/web/internal_ingress_app.py) accepts the native `wwebjs` payload with a shared HMAC and active-tenant lookup | Add a distinct, provider-neutral ingress operation with a credential bound to one tenant/integration. |
| [Neutral bridge](../attention_router/application/platform/integrations.py) converts contracts into core inputs | Define admission before choosing and proving a runtime dispatcher. |
| [Inbound receipt table](../attention_router/infrastructure/models.py) deduplicates `(tenant_id, source, external_event_id)` | Specify neutral admission identity including the integration installation/account, without changing the existing table here. |

The first operation is **connector to Andy, `inbound_event` only**, for a bound
`CHANNEL` or `CAPABILITY` source. A provider event is still input, not permission
to execute a capability. A connector normalizes native provider data before this
operation. Public provider webhooks remain a separate boundary.

| Wire family | Direction and authority boundary |
| --- | --- |
| `inbound_event` | Proposed admission operation below; connector credential establishes integration identity only. |
| `artifact_receipt` | Future registration operation through the existing Artifact Plane and scoped storage/provenance checks; no upload or access grant here. |
| `channel_delivery` | Future Andy-to-connector egress after existing policy, approval and execution authorization. |
| `capability_invocation` | Future Andy-to-provider egress with authorized capability/resource scope. |
| `integration_result` | Future return path must match a server-issued pending operation, bound integration and tenant; unsolicited results cannot prove execution or delivery. |

There is no generic dispatch of all five families. Outbound requests, result
callbacks, discovery, OAuth flows, streaming, batching, uploads and SDK retries
are not implemented in this increment.

## Request profile

| Item | Proposed rule |
| --- | --- |
| Operation | `POST /api/v1/ingress/integrations/events` on the ingress surface, separate from admin/public-provider routes. |
| Transport | HTTPS with certificate/hostname verification. Do not send credentials to HTTP or follow redirects. |
| Authentication | Exactly one `Authorization: Bearer <opaque credential>` header. No token in URL, query, cookie, body or alternate tenant header. The placeholder is not a credential. |
| Body | One UTF-8 JSON object; `Content-Type: application/json`, optionally `charset=utf-8`; no content encoding/compression. |
| Limits | At most 65,536 body bytes, enforced while reading, including chunked requests; maximum 64 nested object/array levels, root at level 1. |
| Wire validation | Canonical V1 schema, with `contract_type: "inbound_event"` and `schema_version: "1"` explicitly present; enforce formats without coercion, defaults or field removal. |
| Parsing | Reject invalid UTF-8/JSON, non-finite numbers and duplicate object member names at every depth before model construction. Preserve the original body bytes. |
| Tenant | Required in the wire body as an assertion; must exactly equal the authenticated binding's tenant. No default, trimming, case folding or Unicode normalization. |
| Idempotency | Use the body's `idempotency_key`; no second key in a header. Persist the serialized body once and reuse its bytes on retries. |

An `Idempotency-Key`, `X-Tenant-ID` header or any query parameters are rejected
with `400 INVALID_REQUEST`, rather than becoming another source of authority.
Browser cookies/admin tokens and legacy HMAC headers do not authenticate this
route. Duplicate Authorization headers or mixed credential mechanisms are
`400 INVALID_REQUEST`; missing or invalid credentials are `401`.

The proxy/application deployment must preserve body bytes and reject ambiguous
HTTP framing. A TLS terminator is part of the trusted boundary: protect its hop
to the application and prevent direct untrusted access to that hop. Forwarded
headers never supply a principal, tenant or audience. Do not enable a development
authentication bypass. Test harnesses may invoke the handler without a network.

The stricter HTTP parsing/size profile does not alter the general contract SDKs.
Their current JSON parsers do not prove duplicate-member rejection, and their
serializers are not a shared canonicalization format. The future server owns
these additional checks; the client must document the supported profile.

For a server binding to tenant `00000000-0000-4000-8000-000000000001`, source
`CHANNEL/channel.email`, instance `synthetic-mailbox` and account
`synthetic-account`, this is a synthetic request body. Its fields assert that
binding; they cannot create or authenticate it:

```json
{
  "contract_type": "inbound_event",
  "schema_version": "1",
  "tenant_id": "00000000-0000-4000-8000-000000000001",
  "source": {
    "kind": "CHANNEL",
    "name": "channel.email",
    "instance_id": "synthetic-mailbox",
    "account_id": "synthetic-account"
  },
  "external_event_id": "synthetic-event-1",
  "event_type": "message",
  "payload_type": "EMAIL_REFERENCE",
  "payload_ref": {"message_ref": "synthetic-opaque-ref"},
  "artifact_ids": [],
  "occurred_at": "2026-09-11T20:00:00Z",
  "received_at": "2026-09-11T20:00:00Z",
  "idempotency_key": "synthetic-admission-1",
  "correlation_id": "synthetic-correlation-1"
}
```

## Authenticated binding

The following are logical server records, not a database migration or public
provisioning API:

| Record | Required meaning |
| --- | --- |
| Integration binding | Stable `binding_id`, trusted deployment `audience`, existing `tenant_id`, exact `source.kind`, `source.name`, `source.instance_id`, optional `source.account_id`, active/disabled state and allowed scopes. |
| Credential | Safe audit identifier, hash of an opaque secret with at least 32 cryptographically random bytes, one immutable `binding_id`, activation/expiry times, revoked state and scopes. No raw secret stored in the registry. |
| Effective permission | Intersection of credential and binding scopes; `integration:inbound_event:write` is required for this operation. No wildcard or implicit admin permission. |

A binding belongs to exactly one tenant and one integration tuple. One credential
cannot choose among tenants/accounts. A multi-account connector obtains separate
bindings and credentials. An absent or `null` account means **no account** and
matches only a binding with no account; it never means any account. A non-null
account must match exactly. Source strings are compared after wire validation,
without native model normalization or prefix/substring matching.

The deployment audience comes from trusted server configuration and must equal
the credential's binding audience. It is not taken from `Host`, request JSON or a
forwarded header. Credentials issued for a different environment are invalid.

Only a separately authorized administrative/provisioning operation can create,
disable or rotate these records. Ingress cannot create tenants, enroll itself or
update its binding. Provisioning must establish control of the integration
installation/account before enabling the binding; naming a provider account is
not proof of ownership. Provisioning mechanics are a later implementation gate.

The tuple `(audience, tenant_id, kind, name, instance_id, account-or-no-account)`
has one stable binding identity, including its history. Re-enrolling that same
tuple must not mint a fresh replay namespace. Changing a bound tenant/source is
not credential rotation; it requires a distinct, explicitly authorized binding.

Resolve opaque credentials by a protected hash lookup; never log their values or
hashes. Enforce activation, finite expiry and revocation using server time. Fail
closed if the registry or tenant state cannot be read. Revalidate credential,
binding, scope and active tenant in the admission transaction. Admission and
disable/revoke operations must serialize against the same authoritative state:
after revocation commits, no later admission or duplicate lookup may succeed
using that credential. A stale positive cache is insufficient.

Rotation issues a new credential for the **same** binding, with a bounded overlap
and explicit old-credential revocation. Existing receipt identities survive
rotation. Revoking a credential blocks subsequent admission, including retries;
it does not retroactively cancel accepted work. Disabling the binding or tenant
also blocks pending work from starting. A later dispatcher must recheck those
states; it must not undo an already completed external effect.

Bearer possession authenticates the connector, not the human sender or original
provider end to end. A stolen valid credential can fabricate events within its
binding until revoked. Idempotency limits repeated admission; it does not detect
an attacker inventing fresh event IDs. Do not conflate this with message signing
or end-user authentication.

## Admission order and authority

1. Enforce transport/header/body bounds; reject ambiguous credentials/framing.
2. Authenticate the credential and its deployment audience. Invalid credentials
   reveal no tenant, source, receipt or payload validation details.
3. Parse and validate the wire object against the profile and inbound family.
4. Resolve the binding and require its tenant/source tuple, scope and active
   tenant to match. Mismatches are `403 BINDING_FORBIDDEN` without indicating
   whether another tenant/account exists. Do not replace a mismatched body field
   silently. Internal operations use the bound tenant, not an independently
   passed caller tenant.
5. In one transaction, recheck authoritative state, decide duplicate/conflict and
   persist new admission plus recoverable pending work. Commit before a success
   response. No external call or engine execution occurs inside this transaction.

No receipt lookup happens before authentication and binding authorization.
Validation/error responses contain stable codes and a server-generated request
identifier, never rejected values, secrets, raw bodies or database exceptions.
Audit successful decisions by credential ID, binding ID, bound tenant, receipt
and server time; pre-auth failures get only a request ID and safe reason code.

The body remains untrusted input after admission. External actor/thread IDs are
resolved within the bound tenant **and integration/account namespace** before
any canonical identity is attached. Receipt IDs, correlation/causation IDs and
`metadata_sanitized` are not permissions. In particular, `from_me`, owner flags,
actor categories and approval-looking metadata cannot grant owner/admin status,
human approval, synthetic-lineage authority or execution permission.

`payload_ref`, artifact IDs and storage references are opaque claims. Admission
does not fetch URLs, read files, upload media or grant resource access. Any later
consumer must resolve references using scoped server records and the existing
Artifact Plane/policy checks. Never dereference an arbitrary URI from a payload.

## Durable identity and retries

The new admission store must preserve two uniqueness constraints:

- `(bound_tenant_id, binding_id, contract_type, idempotency_key)`;
- `(bound_tenant_id, binding_id, contract_type, external_event_id)`.

The stable binding includes source kind/name/instance/account. Neither the
credential ID nor `correlation_id` selects a replay namespace. The second key
prevents a connector from admitting the same external event again with a new
idempotency key. Separate bindings/tenants may legitimately use identical keys.
`external_event_id` identifies an event occurrence, not a mutable provider entity;
distinct updates to one entity need distinct event identities at normalization.

Store a SHA-256 fingerprint of the exact validated request body bytes, together
with its durable receipt and recoverable work. This is a conflict fingerprint,
not a signature. Do not hash a reserialized/native-normalized object. On replay:

- Same identity and identical body bytes: return the original receipt without
  adding work, whether it is pending, processed or rejected by later policy.
- Either identity collides but the bytes differ: `409 IDEMPOTENCY_CONFLICT`;
  leave the original receipt/work unchanged and create no second work item.
- If the two keys refer to different existing receipts, also return `409`.

Database constraints and transactions must decide concurrent races; an in-memory
cache or SELECT-then-INSERT check alone is insufficient. A receipt and its
recoverable pending work commit atomically, possibly as one durable inbox row.
A crash before commit yields no admitted work; a crash after commit but before
response allows a retry to recover the original receipt. Worker redelivery still
requires downstream idempotency and the existing execution/delivery safeguards.

Byte equality is deliberately strict: whitespace, key ordering, absent versus
explicit-null account, timestamps and correlation changes can cause a conflict.
The future sender retains the original serialized bytes across timeout, restart
and credential rotation. Correcting a conflicting event is a reconciliation
decision, not an automatic retry with a newly generated key.

V0 permits no automatic expiry of deduplication identities while a binding can
admit traffic. Payload retention may be shorter, but receipt/key/digest tombstones
must continue preventing readmission. A different retention/replay window needs
an explicit protocol decision before deletion is enabled.

## Responses

Every application response uses `Cache-Control: no-store`. Success and error
objects are separate transport envelopes, not `IntegrationResultContract`.

| HTTP | Meaning / stable error code | Sender behavior |
| --- | --- | --- |
| `202` | New durable admission, `status: "accepted"` | Record receipt; no delivery/execution claim. |
| `200` | Previously admitted identical request, `status: "duplicate"` | Use original receipt; no new work. |
| `400` | `INVALID_REQUEST`: invalid JSON/UTF-8, duplicate members, nesting limit or ambiguous headers/query | Correct the request; no automatic retry. |
| `401` | `UNAUTHENTICATED`: missing/invalid/expired/revoked credential or wrong audience | Obtain a valid credential for the same binding before retrying; no auth fallback. |
| `403` | `BINDING_FORBIDDEN`: tenant/source/account mismatch, insufficient scope, disabled binding or non-active tenant | Correct authorized configuration; do not probe other tenants. |
| `405` | `METHOD_NOT_ALLOWED` with `Allow: POST` | Use the declared operation. |
| `409` | `IDEMPOTENCY_CONFLICT` | Reconcile; never generate a new key automatically. |
| `413` | `BODY_TOO_LARGE` | Reduce/restructure input; no binary upload through this operation. |
| `415` | `UNSUPPORTED_MEDIA_TYPE`: media type, charset or content encoding | Use the declared JSON profile. |
| `422` | `INVALID_CONTRACT`: wrong family/version or schema/format failure | Correct the contract; valid JSON alone is insufficient. |
| `429` | `RATE_LIMITED` | Respect `Retry-After` with bounded backoff/jitter; reuse the body. |
| `503` | `INGRESS_UNAVAILABLE`: registry, durable store or admission capability unavailable | Bounded retry with original body; respect `Retry-After` when supplied. |

For `401`, include `WWW-Authenticate: Bearer realm="andy-integration-ingress"`;
add `error="invalid_token"` only when an invalid token was supplied. For a scope
failure, the `403` challenge may report `error="insufficient_scope"` without
disclosing binding data.

A success body contains exactly these required fields: `transport_version`
(constant `"1"`), `status` (`"accepted"` or `"duplicate"`), `receipt_id`
(server-generated nonempty string, maximum 64 characters), `admitted_at`
(server UTC date-time) and `correlation_id` (the original contract value).
Only `status` changes on a successful duplicate response. Caller `received_at`
and `occurred_at` remain event claims; they are not the server's admission clock.

```json
{
  "transport_version": "1",
  "status": "accepted",
  "receipt_id": "00000000-0000-4000-8000-000000000099",
  "admitted_at": "2026-09-11T20:00:01Z",
  "correlation_id": "synthetic-correlation-1"
}
```

An application error body contains exactly `transport_version: "1"`, an
`error_code` from the table and `request_id` (server-generated nonempty string,
maximum 64 characters). A `401` without credentials carries no extra diagnostic
detail. Transport/proxy failures may have no JSON body: clients must handle that
as an HTTP/connection failure, not a malformed provider result.

Timeout, connection loss, unrecognized `5xx`, or an invalid/missing success body
leave the outcome unknown. Retry only with the same body/key, within bounded
attempt/time limits. No retry loop is implemented here. An acknowledgement is
never permission to claim a reply was sent. There is no status-polling operation
in this first profile; later completion evidence needs its own scoped contract.

## Synthetic acceptance matrix

These are requirements for the complete transport. The
[offline proof coverage](integration-tenant-binding-proof-v0.md) distinguishes
implemented binding checks from future HTTP/durability scenarios. Let A and B be
synthetic active tenants; E and W
are their independently provisioned email/WhatsApp integration bindings.

| ID | Scenario | Required observation |
| --- | --- | --- |
| B01 | Valid credential A/E and matching V1 event | One receipt and recoverable work item; `202` only after commit. |
| B02 | Valid A/E credential, body names active B | `403`; no B receipt lookup, work or disclosed existence. |
| B03 | Change kind, name, instance or account independently | `403` for each mismatch; no normalization that hides a mismatch. |
| B04 | Omit tenant; use blank/incorrect tenant | Missing/invalid shape gives `422`; schema-valid mismatch gives `403`; no default. |
| B05 | Account absent/null versus a bound non-null account | `403`; absent/null matches only an accountless binding. |
| B06 | Missing, invalid, expired, not-yet-active, revoked or different-audience credential | `401`; no receipt lookup or payload detail disclosure. |
| B07 | Tenant/binding disabled or effective scope absent | `403` even for a previously admitted duplicate. |
| B08 | Duplicate auth header, mixed auth, tenant/idempotency header or query | `400`; no alternate source of authority. |
| B09 | Registry unavailable or stale positive cache after committed revocation | No acceptance through cached authorization; fail closed (`503` for unavailable state). |
| B10 | Credential rotation for the same binding | New valid credential returns original receipt for same bytes; revoked old one gets `401`. |
| B11 | Same body/key delivered serially and concurrently | One durable work item; all successful replies refer to the same receipt. |
| B12 | Same key with changed body; same external ID with a new key | `409` in both cases, including concurrent requests. |
| B13 | Same IDs under separately authorized tenants/instances/accounts | Independent receipts, no collision or cross-read. |
| B14 | JSON reserialization, changed correlation/timestamps, or absent-to-null change on retry | `409`; original receipt remains intact. |
| B15 | Crash before commit; crash after commit before response | Zero admission in first case; retry recovers the single receipt in second. |
| B16 | Admission racing revocation/tenant disable | Serialized outcome; no admission after the disabling transaction wins. |
| B17 | Body over 64 KiB with/without Content-Length, excessive nesting or compression | Bounded rejection (`413`, `400` or `415` respectively), no buffered unbounded body/work. |
| B18 | Nested duplicate JSON keys, bad UTF-8, NaN/Infinity, unknown fields or wrong version/family | Parsing failures `400`; schema/family failures `422`; no constructor defaults. |
| B19 | Malformed body together with invalid credentials | After cheap transport bounds, `401` before payload validation details. |
| B20 | Owner/approval/from-me claims; guessed actor/artifact IDs; arbitrary payload URI | Admission grants no authority; consumer cannot promote claims or read another scope. No URI dereference during admission. |
| B21 | Replay after payload cleanup or re-enrolling the same tuple | Tombstone/stable binding prevents fresh admission. |
| B22 | Worker redelivery or binding/tenant disable before dispatch | Existing downstream safety applies; disabled scope starts no new work; no false delivery evidence. |
| B23 | Timeout/429/503 and a redirect to another origin | Future client retries boundedly with stored bytes where applicable; never forwards credentials on redirect. |
| B24 | Attempt to use new credential at admin, legacy HMAC or outbound surface | Credential grants no authority on another surface. |

## Implementation gates and next safe increment

1. The [offline credential-to-binding proof](integration-tenant-binding-proof-v0.md)
   implements synthetic A/B tenant, integration/account, lifecycle, scope and
   mismatch cases. It does not establish registry freshness or durable admission.
2. Implement the bounded receiver and durable admission with PostgreSQL unique
   constraints, concurrency, rollback/crash and revocation tests. Freeze a
   machine-readable HTTP request/response contract against those tests. Pass the
   existing seven required CI gates for that implementation.
3. Only then add thin Python/TypeScript HTTP clients with synthetic server tests
   for auth isolation, byte-preserving retries, timeout ambiguity and redirects.

Runtime dispatch remains an explicit gate before production intake is enabled.
The [current native DTO](../attention_router/adapters/inbound.py) requires actor
and content and limits event types/identifier lengths differently from the V1
wire event; the current database receipt also has narrower fields. The neutral
bridge alone neither persists the request nor resolves those differences. Do not
truncate IDs, invent actors/content, discard references or reuse a narrower
deduplication scope to make the new route appear functional.

A later dispatcher must prove a lossless mapping for each supported event
profile and scoped actor/artifact resolution, preserve the original neutral
identity, and leave existing authority/delivery gates intact. Unsupported
profiles cannot be silently acknowledged and discarded; production admission
stays disabled until its durable recovery and consumer path are demonstrable.
No WhatsApp migration, new provider activation, execution redesign, provisioning
UI, RLS rollout or package publication is part of this design increment.
