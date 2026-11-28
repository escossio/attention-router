# M1 — authenticated experimental memory

Dependent on backend #480 at `bd7d3f4008d2bb98a81c7bd5eaa49ccaac507865`.
Tracking: #479, authorization comment 7243560467. No M0 changes.

## Composition

`create_app(issuer_factory=..., reader_factory=..., settings=...)` returns an
**unbound ASGI app**. There is no server launcher, implicit DB URL, default
credential, production route registration, runtime migration or deployment.
Factories must target the same dedicated database with distinct, unprivileged
roles. Startup attests database name, synthetic marker, exact 12-table schema,
read-only query role and enabled write fences. Supply existing project dependencies.

1. Reuse `ClientSessionService` and `build_client_session_router` unchanged.
2. A synthetically pre-enrolled P-256 device requests a challenge, signs the exact
   challenge bytes and completes issuance. Persist only the existing token digest.
3. POST `/api/v1/client/context/query` with `Authorization: Bearer <session>` and
   `{"scope_ref":"ctx_<64 hex>","question":"Qual é a cor do carro de Roberto?"}`.
   No caller-supplied tenant/OWNER is accepted. Resolve them from current authority.
4. `MemoryAuthority.guard` reuses `authenticated_bootstrap`, requires current OWNER,
   active tenant/device/session, a nonexpired explicit synthetic scope and allowed IDs.
5. `answer_authorized` retrieves current V0 evidence only for that scope.
6. `AuthenticatedMemoryDelivery.query_and_deliver` satisfies the existing
   `StagedDelivery` protocol structurally. It captures, computes outside the
   transaction, then independently revalidates authority, scope and exact evidence.
7. Its final transaction encloses **actual ASGI send**, not construction of a
   Response. Statement-level writer triggers acquire an exclusive advisory fence;
   delivery acquires its shared counterpart. Corrections/revocations serialize with
   disclosure. This deliberately coarse lock is confined to the disposable DB.

No ClientContextReader, fabricated graph/media receipts, context compiler, Outbox,
executors, embeddings, external model or cognitive migrations. The productive
StagedContextEndpoint and its canary guards are neither imported at runtime nor modified.
No APP_ENV condition relaxes authority. Existing ORM imports can construct the
project's lazy engine object, but this composition never opens that connection;
all executed queries use explicitly supplied experimental factories.

## Persistence and provisioning

`create_disposable_schema` only accepts a new empty `andy_memory_m1_<32 hex>` DB.
It creates six existing identity/session tables individually, the original three
V0 memory tables verbatim, and three experimental scope/marker tables. No global
`Base.metadata.create_all` or Alembic is invoked. It must never target the M0 DB.
`register_scope` is an administrative, caller-transaction-owned operation, requiring
an active OWNER and explicit information IDs; there is no provisioning HTTP route.

Separate issuer/query DB roles. Query role has SELECT only. Session issuance needs
INSERT/UPDATE on challenges/sessions and `UPDATE(id)` on device/membership/tenant
solely because existing issuance uses `SELECT FOR UPDATE`; no HTTP mutation of
those IDs is exposed. Credentials remain local, never in configuration examples.
Administrative provisioning is trusted and not exposed to HTTP clients. No grants,
DDL or synthetic enrollment happens in `create_app`.

The CI fixture creates and removes a unique database and two unique roles inside
the disposable PostgreSQL cluster. It enrolls only a generated synthetic device;
this **does not implement or authorize physical-device enrollment**. It then uses
real challenge signing and session HTTP routes, without bearer fixtures or fake auth.
The V0 engine is reused unchanged, with its connection factory pointed only to
that disposable DB inside the test. Fixtures never inspect the M0 bench.

## Contract and failures

Success remains V0 contract 1: status, answer, evidence entries (information ID,
evidence ID, revision, excerpt, origin, registration time) and correlation ID.
HTTP 200 supports ANSWERED, INSUFFICIENT_EVIDENCE and AMBIGUOUS.
Errors disclose no evidence: 401 invalid/expired session, 403 authority/scope denial,
409 changed evidence, 422 invalid input, 503 infrastructure failure. Error envelopes
use UNAVAILABLE plus a machine-readable error code, never false abstention.
`Cache-Control: no-store`; success correlation matches `X-Correlation-ID`.
Input 8 KiB/question 1000 characters; output 16 KiB; request/send 2 seconds;
DB statement 3 seconds and lock 1 second. Do not log bearer, question or excerpts.

## Validation and Android handoff

Run the PostgreSQL test module **on the existing distributed workers**, at the
published exact SHA. Do not start local PostgreSQL or bypass worker capacity/gates.
New tests live in `tests/integration/test_incremental_memory_m1.py` and intentionally
do not use the former migration fixture. Existing session and M0 tests remain intact.
A pass must include success, expiry/revocation, wrong tenant/scope, correction race,
held delivery fence, historical preservation, abstention, readonly grants, missing
fence rejection and infrastructure errors. CI evidence is separate from code existence.

Android #105 was legitimately merged by ChatGPT into main
`3c3f34da8cd1dd9e7f7efdce398514a35a90dee3`. Candidate #104 has been replayed onto
that base and composes the contextual SDK/coordinator/ViewModel/panel as governed by #103. Require
`.lab`, visible lab indicator, exclusive HTTPS endpoint and experimental identity
storage. Missing config fails closed; no productive endpoint/command-channel fallback;
no redirects; cancel pending work on session/tenant/identity change. Text first.

Future operational proposal (not activated): dedicated TLS termination, restricted
experimental ingress, no production network memberships, separate identity/session
persistence and explicitly provisioned synthetic device/OWNER/scope. Server-held
scope is supplied via an approved lab configuration, never inferred from a phone's
asserted tenant. Physical-device enrollment needs its legitimate existing enrollment
flow and separate experimental identity configuration before any installation.
No endpoint, TLS, ingress, APK or device onboarding is enabled by this PR.

Rollback: discard this composition and its disposable CI DB/roles. No M0 or
productive schema/config migration must be reversed. G3 NO_GO; G6 pending.
