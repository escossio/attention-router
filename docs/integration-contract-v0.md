# Integration Contract V0

Integration Contract V0 is the provider-neutral boundary between Attention Router and external channels/capability providers.

The architectural rule is **contract first, SDK second, adapters third**. The contract must not depend on Gmail, WhatsApp, Telegram, Google Calendar, Home Assistant, or any other provider-specific payload shape. Provider adapters translate native APIs into this contract; future SDKs package the repetitive mechanics around the same versioned schema.

## Contract families

V0 publishes five message families through one discriminated JSON Schema:

- `inbound_event`: an external channel/provider event entering Attention Router;
- `artifact_receipt`: provenance and storage identity for a received file that feeds the existing Artifact Plane;
- `channel_delivery`: an authorized delivery request destined for a communication channel;
- `capability_invocation`: a semantic request to a capability provider;
- `integration_result`: standardized success/failure/retry semantics returned by integrations.

The language-neutral schema is committed at `contracts/integration/v1/integration-contract.schema.json`. Python models are in `attention_router/contracts/integration.py`, and CI asserts that the checked-in schema exactly matches the generated model schema. A TypeScript or other SDK must implement the JSON Schema contract rather than importing Python internals.

## Portable validation and native normalization

The public wire schema is produced by `integration_contract_json_schema()`. It adds
semantic rules that Pydantic's automatic export does not encode: channel versus
capability roles, coherent result/error/retry fields, canonical names and SHA-256,
nonblank integration identifiers, and unique, already-trimmed artifact IDs. Wire
messages explicitly include `contract_type` and `schema_version`.

Python constructors remain normalizers for trusted adapter inputs. For example,
they can lowercase a SHA-256, uppercase a reason code, supply a version default or
convert a timestamp to UTC. Their permissive input surface is **not** the public
wire contract. Future SDKs must validate incoming JSON against the published
schema before constructing native models. Validators must enforce `date-time`
formats and must not coerce types, inject defaults or remove unknown fields.
Knowing that JSON is valid still grants no authority and establishes no tenant
binding.

The shared corpus at `contracts/integration/v1/conformance-cases.json` records exact
synthetic JSON messages, expected wire acceptance and expected native Python
acceptance. It distinguishes constructor normalization from portable validation
and covers all five message families, including the semantic gaps above, Unicode
whitespace and timestamp edge cases. Explicit character classes avoid JS/Python
whitespace differences; timestamps exclude year zero and leap seconds, which the
native models cannot represent. Python
`jsonschema` and JavaScript Ajv validate the same corpus independently; successful
native construction must serialize back into valid wire data. This establishes a
tested interoperability boundary, not a proof over every possible JSON value.

Run the focused checks with the development dependencies installed:

```bash
python -m pytest -q tests/test_integration_contract.py tests/test_integration_contract_conformance.py tests/test_integration_adapter_proof.py
npm --prefix contracts/integration/conformance ci --ignore-scripts
npm --prefix contracts/integration/conformance test
```

The JavaScript check runs inside the existing required `transport-tests` CI job;
Python conformance cases run inside `python-tests`. The JavaScript package is a
private test harness, not a published SDK. The JSON Schema discriminator annotation
is informational; standard `oneOf` and `const` constraints enforce message family
selection.

This is a correction to the public prerelease V1 schema. Consumers of the earlier,
permissive export must adopt the corrected validation rules. Existing normalized
reference-adapter outputs remain covered by regression tests; native constructor
normalization and runtime transport behavior are unchanged.

## Tenant and identity boundary

Every operational contract carries an explicit `tenant_id`; there is no default tenant in this boundary. The tenant must be established by a trusted server-side binding/authentication layer. A public webhook body must never be allowed to choose an arbitrary tenant merely by supplying a `tenant_id` field.

Provider actor/thread identifiers remain external references. They are not automatically promoted to canonical Attention Router actor IDs. The bridge to `EventEnvelope` only uses an actor ID after the runtime has resolved that identity inside the tenant.

## Artifacts

The integration layer does not invent another file store. `artifact_receipt` maps into the existing Artifact Plane (`ArtifactReceiptInput`), retaining SHA-256 content identity, source provenance, storage reference and receipt identity. A connector can therefore receive the same content through different channels while the Artifact Plane keeps one tenant-local artifact with multiple receipts.

Storage references are opaque internal references, not public download URLs or access grants.

## Capabilities and authority

`capability_invocation` maps to the existing semantic `CapabilityRequest`. This contract describes **what is requested**, not whether execution is authorized. Capability grants, approval, policy and execution gates remain separate Attention Router responsibilities.

Likewise, `channel_delivery` is a delivery contract, not an authorization grant. A connector must never treat possession of a well-formed delivery payload as proof that the action was authorized.

## Idempotency and retry

Inbound events and outbound/invocation requests carry explicit idempotency/correlation identifiers. `integration_result` standardizes four outcomes: `SUCCEEDED`, `ACCEPTED`, `RETRYABLE_FAILURE`, and `PERMANENT_FAILURE`. Retryable failures can carry `retry_after_seconds`; failure results require a normalized error class.

## Privacy and prompt boundary

External actor/thread/provider identifiers are intentionally not copied into canonical prompt-facing metadata by the bridge. Only sanitized, low-risk integration metadata such as integration name, kind, thread kind and artifact count is propagated. Provider-specific raw payloads should remain behind opaque references.

## What V0 deliberately does not do

V0 does not implement Gmail, Telegram, calendar, object upload APIs, OAuth, SDK packaging, connector discovery, webhooks, runtime delivery, or provider credentials. It also does not replace the existing WhatsApp transport. The next proof should use two materially different adapters — the existing WhatsApp path and an email adapter — to verify that both can satisfy the same contract without adding provider-specific conditionals to the core.

Once that seam is proven, the repetitive client behavior (schema validation, auth/signing, tenant assertion, idempotency, retries, correlation, artifact handoff, health/observability) can be extracted into **Andy Integration SDKs**, initially Python and TypeScript.
