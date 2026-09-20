# Personal Context V1Q — Bounded Structural Context Review

Status: implementation candidate

Related:
- #84 Personal Context V1
- Context Retrieval V0
- V1N governed missing-step anomalies
- V1O proposal-only anomaly suggestions
- V1P suggestion delivery and reply lifecycle

## Purpose

Allow an authenticated owner who already expressed interest in a V1P suggestion
to request a minimal explanation of that exact suggestion without opening
general Personal Context retrieval or granting broader disclosure authority.

Target:

`delivered suggestion -> INTERESTED -> explicit "mostrar revisão" -> REVIEWED`

## Two-step disclosure boundary

V1P deliberately defines INTERESTED as interest only.

V1Q preserves that contract.

The owner must issue a second explicit authenticated self-chat command:

`mostrar revisão`

Accepted deterministic variants are intentionally narrow:

- mostrar revisão
- mostrar essa revisão
- ver revisão
- accentless equivalents

The command does not overlap with:

- V1P `quero revisar` / `não quero revisar`;
- V1I executable recommendation `sim` / `não`;
- ordinary owner-control commands.

An active owner-control clarification retains precedence.

## No free-form retrieval

V1Q does not call Context Retrieval V0.

Context Retrieval remains a knowledge-selection boundary and explicitly does
not grant disclosure authority.

Instead, V1Q follows only the exact source IDs already bound into the
INTERESTED suggestion:

1. exact V1N missing-step anomaly claim;
2. exact V1M EVENT_SEQUENCE source claim.

No lexical search, broad memory scan, nearest-neighbor retrieval, or raw
provider lookup is performed.

## Source revalidation

Immediately before disclosure, V1Q requires:

- authenticated OWNER_COMMAND self-chat;
- exact active owner binding;
- exactly one ACTIVE + INTERESTED suggestion;
- suggestion non-secret and unexpired;
- capability_name = null;
- execution_requested = false;
- grants_authority = false;
- source anomaly still ACTIVE, non-secret and unexpired;
- source EVENT_SEQUENCE still ACTIVE, non-secret and unexpired;
- both source claims belong to the same represented actor;
- both source semantic contracts remain INFERRED/HYPOTHESIS.

Any failure is fail-closed.

If the source chain changed after INTERESTED, no additional context is revealed.

## Structural-only disclosure class

V1Q renders only a structural summary derived from already-governed aggregate
fields:

- number of learned routine occurrences;
- approximate support percentage;
- approximate typical A→B interval;
- statement that the expected second step was absent inside the learned window.

The rendered review explicitly excludes:

- message text;
- raw provider payloads;
- locations or coordinates;
- contact names or addresses;
- relationship/resource/signature values;
- source/provider names;
- internal claim/event IDs;
- exact timestamps;
- secrets or credentials.

The lifecycle snapshot records:

`review_disclosure_class = STRUCTURAL_ONLY`

## Lifecycle

A successful review versions the suggestion:

`INTERESTED -> REVIEWED`

The new snapshot preserves:

- capability_name = null;
- execution_requested = false;
- grants_authority = false.

It records only:

- review_inbound_event_id;
- reviewed_at;
- review_disclosure_class;
- the already-rendered structural summary.

The previous INTERESTED snapshot remains preserved as SUPERSEDED history.

## No implicit action

REVIEWED is not an execution state and is not a capability approval.

V1Q creates no:

- ExecutionIntentRow;
- AgentExecutionIntentRow;
- ReminderRow;
- FactRow;
- capability request;
- capability grant;
- provider execution.

The only outbound message is the authenticated owner-control confirmation that
contains the bounded review summary.

## Proof

Tests cover:

- closed deterministic review parser;
- V1P/V1I reply parsers do not consume `mostrar revisão`;
- INTERESTED + explicit review request -> REVIEWED;
- disclosure class is STRUCTURAL_ONLY;
- no execution/reminder/fact side effect;
- review text contains bounded aggregate explanation;
- review text excludes semantic signature values, provider/source labels,
  internal IDs and transport identity;
- review without INTERESTED suggestion reveals nothing;
- source invalidation after INTERESTED reveals nothing;
- SECRET source after INTERESTED fails closed.

## Runtime state

No migration.

No deploy.

No feature flag is enabled by this change.
