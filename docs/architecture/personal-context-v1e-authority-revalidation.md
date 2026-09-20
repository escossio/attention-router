# Personal Context V1E — Accepted Recommendation Authority Revalidation

Status: implementation candidate

Related:
- #84 Personal Context V1
- docs/architecture/personal-context-v1d-recommendation-lifecycle.md

## Purpose

Take an ACCEPTED Personal Context recommendation and independently revalidate the current execution boundary.

Target chain:

`ACCEPTED recommendation -> current policy -> current capability/provider -> current grant -> approval state -> optional inert ExecutionIntentRow`

Acceptance remains evidence of the user's choice. It is not itself a capability grant or execution authority.

## Accepted recommendation boundary

V1E accepts only the current ACTIVE recommendation lifecycle snapshot when:

- lifecycle_state = `ACCEPTED`;
- capability_name = `reminder.create`;
- execution_requested = false;
- grants_authority = false;
- recommendation remains non-expired;
- acceptance provenance contains an authenticated owner inbound event.

Suggested parameters remain bounded to:

- `summary`;
- `trigger_at`.

## Acceptance provenance

The policy context is resolved from the exact ActorBinding that originated the ACCEPT decision.

V1E recovers:

- recommendation `resolution_inbound_event_id`;
- same tenant;
- same source/channel binding;
- same external actor;
- same canonical actor;
- active binding.

This avoids selecting another channel binding for the same owner.

## Policy revalidation

V1E resolves the current policy using the normal policy resolver.

The resolved policy must currently allow:

- `reminder.create`; or
- wildcard `*`.

The exact current `policy_version_id` is captured in the assessment and any prepared intent.

A missing/unresolvable policy produces:

- assessment_status = `POLICY_UNRESOLVED`;
- no ExecutionIntentRow.

## Grant revalidation

The ACCEPT decision is deliberately **not** passed as `owner_authorized=True`.

Capability resolution is invoked with:

`owner_authorized = false`

Therefore an explicit current capability grant is still required.

Active grant evidence is bounded by:

- tenant;
- grantee_type = ACTOR;
- canonical actor;
- exact capability;
- ACTIVE status;
- validity window;
- no unrelated target resource.

Grant IDs are captured in the assessment and prepared intent scope.

## Capability/provider revalidation

The registered current capability definition and version are resolved again.

The resolver independently checks:

- availability state;
- current provider binding and health;
- current grant;
- current policy;
- side effect / default approval policy.

Known-but-unavailable capability or missing version fails closed.

## Approval boundary

Resolver outcomes map to V1E assessment states:

- unknown / provider unavailable -> `CAPABILITY_UNAVAILABLE`;
- policy/grant denial -> `DENIED`;
- current approval required -> `REQUIRES_APPROVAL`;
- current authority ALLOW -> `INTENT_PREPARED`.

V1E does not create HumanExecutionAuthorizationRow.

A future approval frontier may do so explicitly.

## ExecutionIntentRow

Only when the current resolver returns ALLOW may V1E create one neutral:

`ExecutionIntentRow(state = PREPARED)`

This model is explicitly inert:

- no readiness;
- no lease;
- no effect reservation;
- no provider call;
- no outbox;
- no delivery.

The scope freezes semantic evidence:

- schema_version = `personal-context-recommendation-execution-v1`;
- tenant/person;
- recommendation ID;
- accepted recommendation claim ID;
- acceptance event ID;
- capability name;
- capability version ID;
- suggested parameters;
- exact policy ID/version;
- active grant IDs;
- capability resolution status;
- authority result;
- provider instance ID;
- approval_required.

The intent has:

- `frozen_at = null`;
- `authority_profile_id = null`;
- expiry no later than the accepted recommendation.

PREPARED is not executable authority.

Any future freeze/materialization/execution boundary must revalidate current authority again.

## Idempotency

The semantic scope is deterministically fingerprinted.

Re-evaluating the exact same accepted recommendation under the exact same policy/grant/provider/version snapshot reuses the same ExecutionIntentRow.

Authority changes produce a different semantic scope rather than silently mutating the old intent.

## Audit

Every evaluation emits:

`personal_context.recommendation_authority_evaluated`

including:

- recommendation and claim IDs;
- policy/version;
- grant IDs;
- capability status;
- authority result;
- reason code;
- approval requirement;
- provider instance;
- assessment status;
- prepared execution intent ID when present.

## Safety invariant

V1E never creates or mutates:

- ReminderRow;
- OutboxMessageRow;
- AgentExecutionIntentRow;
- FactRow;
- provider execution;
- external delivery.

An ACCEPTED recommendation with missing grant must remain denied.

A current REQUIRES_APPROVAL result must create no ExecutionIntentRow in V1E.

## V1E proof

Tests must prove:

- ACCEPT without policy -> no intent;
- policy ALLOW without grant -> deny, proving acceptance is not grant;
- active grant cannot override policy DENY;
- known capability without provider -> no intent;
- REQUIRES_APPROVAL -> no intent;
- policy + grant + capability/provider ALLOW -> one PREPARED inert ExecutionIntentRow;
- exact authority snapshot is idempotent;
- assessment audit preserves policy/grant/result evidence;
- recommendation lifecycle remains execution_requested=false / grants_authority=false;
- no reminder/outbox/agent execution/fact side effects.
