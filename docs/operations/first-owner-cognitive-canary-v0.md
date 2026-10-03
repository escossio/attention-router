# First-owner Cognitive Canary Profile V0

Tracking: #251  
Parent: #248

## Goal

Enable real cognitive observation for one exact owner scope without widening
delivery, authority, execution, or learned-model boundaries.

## Exact scope

The first rollout combines two coordinates:

- `PERSISTENT_MEMORY_CANARY_BINDING_ID`: exact active
  `ActorBindingRow.id`;
- `PERSONAL_CONTEXT_RUNTIME_CANARY_TENANT_ID` and
  `COGNITIVE_RUNTIME_CANARY_TENANT_ID`: exact tenant IDs.

When a memory canary binding is configured, legacy ingress resolves the active
binding through the existing source + external actor ID + tenant coordinate
when the caller did not already provide a binding. Unresolved or mismatched
bindings fail closed.

## Why tenant count is not a canary

A workload limit such as `COGNITIVE_RUNTIME_TENANT_LIMIT=50` is not an
identity boundary. The first rollout therefore uses an optional exact tenant
filter in addition to bounded limits.

The same rule applies to Personal Context owner scanning.

## Recommendation expiry

Personal Context runtime performs lifecycle expiry before pattern scanning.
When an exact tenant canary is configured, expiry is tenant-scoped too.
Otherwise a nominal canary could still mutate recommendation state in another
tenant.

## Profile

Use:

`ops/profiles/first-owner-cognitive-canary.env.example`

It enables memory capture/ingestion/context, Personal Context detection and the
deterministic Cognitive Runtime while explicitly keeping proactive delivery,
authority/materialization, learned shadow, external delivery and autonomous
execution off.

The profile contains placeholders only. Real IDs and secrets belong in the
managed runtime environment, never in GitHub.

## Activation boundary

This slice prepares and certifies the profile. It does not deploy it.

Live activation is a separate downstream operation against one exact merged
GitHub SHA, with before/after observability and rollback.
