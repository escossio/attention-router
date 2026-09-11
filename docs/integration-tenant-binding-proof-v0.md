# Offline Integration Tenant Binding Proof V0

This increment implements the isolated binding decision described by
[ADR 0020](adr/0020-neutral-ingress-and-authenticated-tenant-binding.md), before
the [neutral HTTP receiver](integration-transport-v0.md). It is not wired into
any application route, worker, provider adapter or SDK.

## Implemented boundary

[`tenant_binding.py`](../attention_router/integrations/tenant_binding.py) takes
an opaque credential, event claims, a trusted registry, an explicit server clock
and a deployment audience. It reads the credential by SHA-256 digest, checks
activation/expiry/revocation, resolves its binding, checks the audience and both
scope sets, compares the event's tenant/source exactly, and reads the **bound**
tenant's active state. A mismatched event never selects a foreign tenant lookup.

The required permission is `integration:inbound_event:write` in both the
credential and binding. The logical server records are immutable and reject
malformed identifiers, mutable/wildcard scopes, non-boolean lifecycle flags and
invalid credential lifetimes. They do not perform provisioning or establish
ownership of an external provider account.

`check_inbound_binding()` returns a `BindingDecision`. `BOUND` contains only the
server-derived credential/binding IDs, audience, tenant and source tuple. The
other outcomes contain a stable code and no identity context. No raw credential,
digest, arbitrary metadata or broader registry permissions are copied into the
result. Hashes are excluded from credential-record repr output.

`BOUND` is a namespace decision for this call, **not** a durable receipt,
execution authorization or a reusable security token. A caller cannot substitute
this object for the engine's actor resolution, policy or human-approval gates.
The object is immutable to prevent accidental mutation, not cryptographically
unforgeable against code already running inside the trusted server process.

Credential representation checks accept the Bearer character set and 43–512
ASCII characters. This is a bounded offline implementation choice for the future
HTTP profile, not an entropy estimator. Trusted provisioning must generate at
least 32 random bytes. Tests generate ephemeral credentials in memory; no issued
credential, production registry or credential provisioning API is committed.

## Validation and trust limits

The function validates only the security projection: the inbound discriminator,
V1 version, tenant and source fields. It does not replace the full V1 JSON Schema
validator or parse HTTP/JSON. Unknown top-level fields, payload references and
other non-security fields still require the receiver's complete wire validation.
For example, a matching tenant/source with a missing correlation ID can establish
a binding but must never be admitted as a valid event by the future receiver.

The future receiver must authenticate before exposing body validation errors,
validate the full wire schema and parsing profile, and run the binding decision
again using authoritative state inside its admission transaction. Native model
normalization must not run before comparing the original tenant/source strings.
Absent/null account matches only an accountless binding; no string is silently
trimmed, case-folded or Unicode-normalized during comparison.

`BindingRegistry` is a trusted Python protocol. Adapters must translate unavailable
or inconsistent state to `RegistryUnavailable`; the decision then fails closed
without returning the diagnostic. Mismatched record identities/types also fail
closed. Unexpected programming errors propagate instead of producing success;
the future HTTP boundary must sanitize them. Tenant, audience, clock and registry
are server inputs, never accepted as authority from a request body or headers.

The test registry is deliberately confined to the test file. Each call reads
state again, and no positive decision is cached by this component. This proves
sequential revocation when the registry supplies current state. It does **not**
prove registry freshness, transaction isolation, concurrent revocation/admission,
stable binding uniqueness after re-enrollment or durable replay protection.
Those remain responsibilities of the future database adapter/admission boundary.

## Acceptance evidence

[`test_integration_tenant_binding.py`](../tests/test_integration_tenant_binding.py)
uses two synthetic tenants and six bindings, including separate accounts,
instances, email, WhatsApp and a capability source. All six positive fixtures
are also checked against the unchanged canonical V1 schema with format checks.

| Transport matrix requirement | Offline evidence and remaining limit |
| --- | --- |
| B01 | Matching credential/claims return `BOUND`; no receipt or HTTP 202 is implemented. |
| B02–B05 | All 36 credential/event binding combinations; exact tenant/source/account matching, missing fields, accountless handling and normalization attacks. |
| B06–B07 | Missing/unknown/malformed credentials, inclusive activation, exclusive expiry, revocation, audience, effective scope and active tenant/binding. |
| B09 | Unavailable/corrupt registry and no component-level positive cache; stale backing stores are not certified. |
| B10 | New credential retains the same binding identity; old credential is denied on the next call after revocation. No receipt replay is claimed. |
| B13 | Independent tenant/instance/account namespaces; durable receipt uniqueness is deferred. |
| B18–B19 | Malformed security fields/other families are denied; credential failure precedes claims inspection. HTTP framing, full wire validation and duplicate JSON keys are deferred. |
| B20 | Owner/approval/from-me/actor/artifact claims do not enrich the returned identity or cause reference access. Consumer identity/resource resolution remains a later proof. |
| B24 | Other wire families cannot use this operation. Authentication on actual HTTP/admin/egress routes is not changed or certified here. |

B08, B11–B12, B14–B17 and B21–B23 require the receiver, durable store, dispatcher
or HTTP client. They are not marked complete by this offline proof.

## Reproduction and next boundary

Run the new proof and adjacent ingress/contract regressions using the existing
development environment:

```bash
python -m pytest -q tests/test_integration_tenant_binding.py tests/test_integration_contract.py tests/test_integration_contract_conformance.py tests/test_integration_adapter_proof.py tests/test_internal_ingress.py
python -m ruff check attention_router/integrations/tenant_binding.py tests/test_integration_tenant_binding.py
```

The existing required `python-tests` CI job discovers these tests automatically.
The current HMAC ingress, runtime, schema and SDK packages retain their existing
behavior. The next implementation boundary is authoritative registry storage
and bounded, transactional admission, with PostgreSQL race/crash tests before an
HTTP client. This proof alone is insufficient to activate production intake.
