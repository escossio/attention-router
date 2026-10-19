# First Contact V1: durable public presentation

Status: disabled candidate. This document defines the implementation contract;
it does not authorize rollout, establish provider delivery or certify production.

## Scope and reuse

The existing Agent decision pipeline, visitor text-only contract, policy/autonomy
gates, ExecutionIntent, transactional outbox and Transport provenance ledger own
the response path. First Contact adds bookkeeping and public identity composition,
not a sender, personality engine, memory system, tool grant or owner identity.

The default is OFF. When enabled, Worker and Transport must agree on the explicit
`FIRST_CONTACT_ELIGIBILITY_MODE` (`INDIVIDUAL` or `OPEN_INBOUND`), tenant and
source account. `INDIVIDUAL` requires the privately provisioned lowercase
SHA-256 of one verified canonical provider LID. `OPEN_INBOUND` omits that peer
pin and admits any authenticated, canonical, organic direct WhatsApp visitor
that passes the existing general `text-only-v1` visitor guard. Both modes require
the same tenant/account scope, `provider-lid-v1` identity proof and persistent
per-peer state. Open eligibility is limited to inbound messages; it is not a
sender, binding, owner, policy, autonomy or tool grant. Bound actors, owner
commands and groups retain their contracts. No policy/autonomy changes make a
presentation deliverable.

Explicit Lifecycle V3 manifests pin both images and the common mode/scope.
During a directed transition the preflight accepts only a live Transport that
matches the pinned source or target pair; read-only `--check` requires exact
target parity.

Live continuity consumes new admitted events. Historical acceleration is
intentionally deferred: short history, generation markers, archived conversation
and bootstrap completion are not authenticated evidence of an introduction
delivered to an interlocutor. No historical backfill, cursor reset or replay occurs.

## Canonical presentation identity

The signed ingress envelope may carry first_contact_identity, contract
provider-lid-v1. A directional provider LID can identify itself. A phone identifier
requires the provider's unambiguous LID/PN mapping corresponding to that exact
directional peer. Multiple LIDs, conflicting phones or unrelated mappings deny
the presentation proof. Display names and arbitrary aliases cannot supply it.

The presentation key is tenant + source + channel + account + SHA-256 of the
verified canonical provider LID. The digest is a pseudonymous operational key,
not an authentication credential or an authorization shortcut. Presentation
version is an attribute, so changing wording cannot silently reintroduce Andy.
Existing bindings on proven aliases deny inaugural presentation; they confer no
new binding/owner authority. Missing identity proof denies the inaugural
lifecycle in both modes. A different canary digest denies it in `INDIVIDUAL`;
`OPEN_INBOUND` relies on authenticated canonical identity plus the tenant/account
scope. In either case, normal eligible text conversation continues under its
existing contract when First Contact is ineligible. A previous ordinary reply
does not count as a First Contact presentation; only the durable V1 state and
correlated delivery evidence do.

## Persistent lifecycle

first_contact_presentations owns the unique scope/current generation/CAS revision.
first_contact_attempts preserves each event, presentation version, decision,
intent, outbox, idempotency key, exact text digest and authenticated delivery proof.
Composite foreign keys prevent cross-scope current-attempt pointers.

| State | Meaning |
| --- | --- |
| RESERVED | One event acquired exclusive presentation ownership; generation may still be pending. |
| QUEUED | The exact generated response, authorized intent and text outbox are linked. |
| SENDING | A durable outbox claim exists before external dispatch. |
| SENT | Local acceptance/DONE or an authenticated Transport acceptance is known; device delivery is unconfirmed. |
| CONFIRMED | Exact authenticated ledger proof has provider message reference and device ACK >=2. |
| FAILED | Failure before the external effect is proved; only a new, newer inbound event may reserve again. |
| AMBIGUOUS | The external outcome is uncertain; automatic resend and TTL takeover are forbidden. |

Generation is a timestamp within RESERVED, not delivery. Same-event replay never
receives a second generation token. Uniqueness plus a short PostgreSQL advisory
transaction lock serializes first reservations; subsequent state writes use row
locks and CAS. Reservations do not expire. A newer eligible inbound can replace
the current RESERVED attempt only when a committed AUTO_ALLOWED evaluation denies
automatic execution and there is no intent, review, outbox linkage or send/proof
marker. The old attempt remains FAILED with its decision and text digest; the
new generation composes the introduction again. DRY_RUN and generated text alone
are not evidence of delivery or a completed denial. Orphan and uncertain-effect
attempts require explicit evidenced reconciliation, never automatic replay. New
presentation locks are released before Agent and HTTP calls; the existing
last-mile owner authority lock is preserved.

Corrupt outbox linkage/metadata is quarantined before Transport. A bookkeeping
integrity failure cannot confirm the presentation or crash the existing worker.
An unchanged response approved through the existing HUMAN_REVIEW path can retain
its presentation linkage; the review must belong to the same decision. This
bookkeeping never creates approval or relaxes the existing release/dispatch gate.
An edited response does not silently inherit the original introduction digest:
the new intent/outbox is quarantined without aborting the worker. Historical
delivery evidence remains true if the review is subsequently revoked.
Receipt polling is bounded and rotates last_probed_at before validation, including
invalid/unknown items, so an old unresolved receipt cannot starve later ones.

## Agent SDK integration

AllowedAgentContext has an optional structured first_contact directive:
INTRODUCE, CONTINUE or UNAVAILABLE. No identity hash, peer, reservation token or
owner information is sent through that directive. Public composition uses the
existing Andy/virtual-assistant identity and shared label. Shared instructions
are retained while the personal owner name is removed for this public context.

The Router adds one short inaugural prefix only for the successful reservation.
All responses in this opt-in public path carry the identity label. The Agent
produces the ordinary body and cannot decide to start another lifecycle.
Unsolicited self-introduction patterns are rejected; this lexical guard is bounded,
not a universal semantic proof about arbitrary natural-language paraphrases.
An explicit identity question can receive a short public identity answer after
confirmation without setting the inaugural generation marker again.

## Authenticated direct delivery evidence

POST /internal/first-contact/delivery is a separate OFF-by-default ledger-only
contract. It neither connects to Browser nor creates the provenance directory.
Group delivery endpoints and policies remain unchanged. Requests and responses
use separate HMAC domains, exact raw bytes, a random nonce and a fresh timestamp.
The Python verifier rejects redirects, stale/mismatched proofs, malformed ACKs
and duplicate JSON fields. The Transport validates its configured tenant/account
and the full effect scope. No arbitrary caller boolean proves delivery.

The 13 correlated fields are schema_version, contract_version, attempt_id,
presentation_version, tenant_id, source, channel, source_account,
canonical_peer_hash, execution_intent_id, outbox_id, idempotency_key and text_hash.
The signed snapshot adds nonce, issued_at, status, message_reference, ack and
ack_at. SENT/ACK0/ACK1 do not confirm. ACK2+ confirms monotonically for that exact
effect; a late ACK may reconcile AMBIGUOUS without replaying it. A new binding
after the historical send does not retroactively revoke a valid delivery proof.

## Schema, compatibility and rollback

0060_first_contact_v1 is additive after 0059_maestro_relay_receipts. The predecessor
is the unchanged historical migration; no MAESTRO runtime or existing table is
rewritten. There is no backfill. Downgrade to 0059 is available only with empty
presentation history; it refuses to erase any durable record. Operational rollback
preserves the new tables and actual delivery/ambiguity history.

TTS, Ogg/Opus normalization and persisted text-only-v1 remain unchanged. Voice
requires a separate approved modality contract, media proof, cost/retry policy
and coverage for the previously identified normalization error path.

Before rollout, independently certify complete lifecycle/Compose parity, exact
artifacts and checks, compatible API/Worker/Transport/schema and a reviewed
targeted rollback. No boot/reconciler or private overlay is changed by this code.
Rollback to a version that ignores the new state must preserve visitor recovery
and pending/ambiguous effects; a delivered message cannot be undone.

## Offline validation

Synthetic unit tests are tests/test_first_contact.py,
tests/test_first_contact_agent.py and tests/test_first_contact_delivery.py.
Transport contracts are whatsapp-transport-local/test/first-contact.test.js.
PostgreSQL wrappers and concurrency/migration tests reside under
tests/integration/test_postgres_first_contact*.py. They use the suite's disposable
database fixtures and mocked Agent/Transport only.

Run heavy PostgreSQL/full regressions on distributed workers. An uncommitted
content bundle may provide preparation evidence where publication is prohibited;
it is not a green required GitHub check or a new certified commit SHA.
