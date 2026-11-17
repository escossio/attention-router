# Minimal supervised semantic canary

Status: review candidate; disabled and unregistered by default. This contract narrows
[the bilateral pilot](andy-whatsapp-semantic-conversation-pilot-v1.md), preserving
D01–D05 and approved architectural directions D06-A/D08-A. CAN-RET-01 approves the numerical reference below for design/tests only.
Operational consent, backup controls and deployment need separate approval.

## Existing components and boundary

One authenticated owner, tenant, WhatsApp account and exact canonical direct
conversation key; explicit observation start/end and message/audio budgets.
`CanaryGrant` has no default TTLs. Each retention layer and the operational,
retention and backup decision references must be supplied explicitly.
A reference string is configuration, not proof that approval occurred.

Transport's optional `SEMANTIC_CANARY_ENABLED`, `SEMANTIC_CANARY_CONVERSATION_KEY`
and `SEMANTIC_CANARY_INGRESS_URL` select a route inside the existing internal
listener, using its existing HMAC and host allowlist. No new listener, service,
container, address or network is required. Transport initiates HTTP to that
listener and uses the existing media store. Network reachability grants no
application authority. Client API review uses existing ClientSession ownership.

The ingress adapter reuses receipt creation and live archive capture. It marks
original rows non-searchable before commit, preserves provider ID/time, technical
origin and semantic attribution, and creates no Interaction, decision queue or
Outbox. D01 attribution grants no command authority. Concurrent automation proof
retains existing classification precedence. First-contact automation must be off
for this selected path. Owner command/self-chat traffic is not adopted.

Media authorization is rechecked before download, retries and storage. Existing
capture receipts prevent duplicate downloads. Separate subdirectories of the
existing spool prevent fallback to ordinary ingress when the flag is off.
The existing STT worker excludes quarantined rows by default; an authenticated
owner processes one selected message using the guarded existing STT implementation.
Policy expiry before or during STT produces a terminal failure without retaining
its late transcript. Capture reconciliation closes expired pending jobs. An
in-flight provider call cannot be cancelled by this adapter: effective revocation
is serialized after that bounded unit of work; no later processing is permitted.

Original archive evidence feeds the existing interpretation/V2E candidate code;
there is no synthetic mirror or second cognitive engine. Owner review, history,
rejection, correction and context reuse the existing governance facade and graph
compiler. The compiler receives only the authorized projection of selected
original messages and provider reply references. Shared legacy materializations
are excluded. ADMITTED means review eligibility, never canonical truth. Automated
and incomplete messages remain explicit evidence states, not human assertions.

## Activation preparation and stop

Deployment wiring remains explicit: construct `GovernedSemanticCanary` with a
current anchored policy journal and ClientSession service, pass it to the existing
internal app factory, and mount the existing review router with
`include_processing=True` on the authenticated Client API. Neither production app
registers these routes automatically. Initialize the grant through the existing
owner-authenticated application method after approval. `enabled=False` is default.
`SyntheticFileAuthority` is rejected outside test environments. A reviewed
operational authority outside the backup restore domain remains a blocker.

Approve one scope, bounded observation, per-class retention and backup disposition
before enabling anything. Verify signed ingress, owner session, stop and revocation
with synthetic fixtures first. Monitor accepted/blocked receipts, media status,
STT status/error, original-message linkage and review/context decisions. Stop on
scope mismatch, unexpected outbound action, duplicate archive, lost provenance,
policy uncertainty or regression in ordinary delivery. Revoke consent and disable
both feature switches while retaining the configured scope and quarantine guards.
Never replay selected spool files through ordinary ingress. Preserve operational
rows and evidence; no purge is implied. Restart only with valid current policy.

Rollback is feature-off with isolation retained. Downgrade to binaries that ignore
quarantine metadata is blocked while canary data exists. Physical deletion is not
claimed: DELETE_DERIVATIVES fences access; VERIFY_DELETION reports remaining rows
and unverified media/backups. Existing synthetic purge tests stay synthetic.

## Priority and acceptance

P0 before activation: exact-SHA review/CI; explicit personal-data consent and TTLs;
operational revocation authority/backup verification; approved deployment and scope.
P1 before wider use: supervised throughput, expiry cleanup verification and provider
latency monitoring. P2: broader language quality, richer review UI and legacy
consumer adoption. P3: GPS, new channels and general historical import.

Live continuity is covered by the isolated selected route. Historical acceleration
is deliberately deferred: no cursor reset, retrospective download or adoption of
already-derived archive rows. Missing audio is context incomplete.

Reproduce acceptance with `pytest -q tests/test_bilateral_integrated.py
 tests/test_semantic_canary_scope.py tests/test_semantic_precertification.py`,
Transport tests via `node --test whatsapp-transport-local/test/*.test.js`, and
`tests/integration/test_postgres_semantic_canary.py` on disposable PostgreSQL using
the distributed workflow. The bilateral canary test emits a synthetic consultable
JSON with original evidence IDs, both transcripts, candidates, review, graph
retrieval and revocation. Corpus reruns are regressions, not new independent
quality certification. No test fixture constitutes real consent or a production TTL.


## Approved canary reference: CAN-RET-01

The optional `canary_reference_rules()` helper returns existing RetentionRule
contracts: MEDIA 7 days; MESSAGE/TRANSCRIPTION 30 days; unreviewed CANDIDATE
7 days; reviewed CANDIDATE derivative 30 days. RetentionSchedule calculates
absolute deadlines from original creation times. No runtime default, consent,
backup rule or durable-memory promotion is introduced. Unspecified classes still
require explicit policy; fixtures for them do not constitute operational approval.

Canary source/context access uses the authorized source/reviewed limits; proposal
creation separately caps its original unreviewed deadline. Explicit owner ADMIT
selects only the reviewed deadline recorded when the proposal was created and
bounded by current source policy. It cannot admit an already expired proposal.
Repeated admission, replay and restore never reset either anchor. An expired
unreviewed proposal stops contributing to contextual retrieval, while original
messages retain their independent authorized 30-day access. At day 30, reviewed
context access fails closed. This is access expiry, not verified physical erasure.

The laboratory's conservative deadline contract and existing non-canary data
remain unchanged. Future authorized historical memory can span months/years;
this short first-canary policy does not replace it. Neither numerical approval
nor owner review authorizes operational activation or canonical promotion.

## CAN-01 control preparation

`CanaryGrant` now enforces the proposed upper bound of 60 minutes, 20 messages and
six audio messages. These bounds are not consent. Initialization still requires
existing ClientSession ownership and actor binding. The journal stores a
`CAN01_OWNER_CONSENT_V1` receipt with authorization time, owner/tenant, operational
decision reference and the digest of the complete grant. Every owner operation and
capture revalidates it; changing scope or retention requires a separately approved
new grant, never silent modification. Original provider times before consent are
rejected. Tokens are never recorded. Earlier canary journals without this receipt
fail closed; there is no automatic migration or fabricated approval.

`ProtectedFileAuthority` reuses the existing atomic file CAS and fsync protocol.
It creates no infrastructure. A private directory with restricted ownership/mode,
explicit restore-boundary evidence and a validity deadline must be supplied.
Placement checks do not establish independence from host snapshots or external
backups: that evidence remains a deployment gate. Loss, corruption, expiry or
rollback blocks access. Bootstrap is an explicit, separately authorized operation;
never initialize an absent authority to recover a failed reader. Do not restore
both the authority and the data from an older snapshot.

`web.app.mount_semantic_canary_review` mounts the existing router only when called
explicitly, with the existing ClientSession service. The internal application
factory already accepts the same service. Neither initializes consent or enables
capture. Keep routes and quarantine guards when disabling the feature. A pending
canary media artifact that never reaches STT becomes terminal at processing expiry,
without changing its original media deadline; existing reference-aware cleanup can
then handle it. No organic purge is added or run by this change.

Live continuity remains scoped, signed and quarantined before commit. Historical
acceleration is deliberately excluded from this first canary: no retrospective
capture, cursor reset or old-source adoption. No new network flow, service,
container, database, model or Android capability is introduced.
