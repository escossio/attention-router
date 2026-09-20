# Personal Context V1D — Recommendation Lifecycle Contract

Status: implementation candidate

Related:
- #84 Personal Context V1
- docs/architecture/personal-context-v1c-recommendations.md

## Purpose

Persist and resolve the lifecycle of a proactive recommendation without executing its mapped capability.

Target lifecycle:

`PROPOSED -> ACCEPTED | DISMISSED | EXPIRED`

User acceptance resolves the recommendation only.

It does not mean:

`ACCEPTED -> capability execution`.

That remains a separate future authority boundary.

## Storage

V1D reuses `MemoryClaimRow`.

Recommendation snapshots use:

- predicate = `context.recommendation.proactive`;
- source_quality = `DERIVED_RECOMMENDATION`;
- status = `ACTIVE` for the current lifecycle snapshot;
- prior snapshot becomes `SUPERSEDED`;
- `supersedes_claim_id` preserves lifecycle history;
- staleness_class = `PERISHABLE`;
- bounded valid_until from the V1C recommendation.

The current lifecycle state is stored in structured JSON:

- PROPOSED
- ACCEPTED
- DISMISSED
- EXPIRED

## Proposal admission

A proposal is persistable only when it remains the exact governed V1C recommendation:

- recommendation_type = `REMINDER_FOR_RECURRENT_ARRIVAL`;
- capability_name = `reminder.create`;
- status = `PROPOSED`;
- requires_user_confirmation = true;
- execution_requested = false;
- grants_authority = false.

The persisted recommendation must still match its source `DERIVED_PATTERN` claim:

- same canonical person;
- active source claim;
- TEMPORAL_RECURRENCE;
- LOCATION_ARRIVAL;
- same confidence;
- same support ratio;
- same occurrence/anomaly counts;
- same provenance classes;
- recommendation expiry cannot outlive source hypothesis.

A forged stronger recommendation must fail closed.

## Explicit user decision

V1D accepts only explicit authenticated owner provenance.

A resolution event must:

- belong to the same tenant;
- be OWNER_COMMAND;
- be owner_authenticated;
- come from the owner self-chat path;
- preserve the existing from_me OWNER_COMMAND classification;
- resolve through an active ActorBinding matching tenant, source, external actor and canonical actor.

The same inbound decision event cannot resolve two different recommendations.

## State transitions

### ACCEPT

Creates a new ACTIVE lifecycle snapshot with:

- lifecycle_state = ACCEPTED;
- resolution event provenance;
- resolution_kind = ACCEPT;
- resolved_at.

The prior PROPOSED snapshot becomes SUPERSEDED.

### DISMISS

Same history semantics, but lifecycle_state = DISMISSED.

### EXPIRE

A due PROPOSED recommendation is superseded by an EXPIRED snapshot.

No user event is required for time-based expiry.

## Idempotency

- exact proposal replay returns the existing PROPOSED snapshot;
- replaying the same terminal decision with the same event is idempotent;
- a conflicting second terminal decision fails closed;
- one decision event cannot be reused across recommendations.

## Execution boundary

Every lifecycle snapshot preserves:

- execution_requested = false;
- grants_authority = false.

V1D does not create or mutate:

- ReminderRow;
- AgentExecutionIntentRow;
- OutboxMessageRow;
- FactRow;
- capability grants;
- provider execution;
- external delivery.

Even ACCEPTED is non-executing.

A future execution frontier must independently:

1. revalidate the accepted recommendation;
2. resolve the registered capability;
3. evaluate current grants/policy/approval;
4. create a bounded execution intent only when authority allows it.

## V1D proof

Tests must prove:

- proposal persistence is idempotent;
- ACCEPT creates a superseding lifecycle snapshot;
- DISMISS creates a superseding lifecycle snapshot;
- explicit authenticated owner provenance is required;
- same decision event replay is idempotent;
- one decision event cannot resolve two recommendations;
- expired proposal cannot be accepted;
- due proposal becomes EXPIRED;
- forged capability/confidence is rejected;
- every transition creates zero ReminderRow, OutboxMessageRow, AgentExecutionIntentRow and FactRow.
