# Personal Context V1R — Governed Owner Context Controls

Status: implementation candidate

Related:
- #84 Personal Context V1
- Personal Context V0 / Context Retrieval V0
- V1H governed owner correction
- V1M governed EVENT_SEQUENCE hypotheses
- V1O–V1Q anomaly suggestion, delivery and bounded review

## Purpose

Allow the authenticated represented owner to control how one reviewed
contextual hypothesis may be used without deleting or rewriting the underlying
evidence.

V1R implements two independent owner policy dimensions:

- privacy;
- actionability.

Target:

`REVIEWED context -> explicit owner control -> durable USER_DECLARED overlay`

## Why an overlay

V1R does not mutate the source hypothesis into a fake fact, delete canonical
evidence, or overload the existing `sensitivity_class=PRIVATE` storage label.

Most governed Personal Context inference already uses PRIVATE sensitivity.
Therefore the owner-facing meaning of “mark this context private” needs a
separate policy contract.

V1R persists a versioned `MemoryClaimRow`:

- predicate = `context.control.owner_policy`;
- source_quality = `USER_DECLARED`;
- target_kind = `PATTERN_HYPOTHESIS`;
- target_hypothesis_id = stable hypothesis identity;
- confidence = 1.0;
- staleness = STABLE;
- grants_authority = false;
- grants_disclosure_authority = false.

No parallel profile/control store is introduced.

## Stable target identity

The control binds to the stable `hypothesis_id`, not to one transient
MemoryClaim snapshot.

This means a later re-inference of the same routine may create a new
MemoryClaim snapshot while the owner policy still applies.

The original and newer inferred claims remain preserved as historical/context
evidence.

## Commands

V1R uses a deliberately closed deterministic vocabulary after a bounded review.

Privacy:

- `marcar este contexto como privado`
- `deixar este contexto privado`
- `remover marcação privada deste contexto`

Actionability:

- `não usar este contexto para ações`
- `marcar este contexto como não acionável`
- `permitir sugestões com este contexto`

Generic yes/no and review/recommendation reply phrases do not match this
control parser.

The exact target is the latest structurally REVIEWED Personal Context
suggestion for the authenticated owner.

## Privacy semantics

Owner privacy means:

- the controlled inferred context is excluded from Personal Context V0 read
  snapshots;
- derived anomaly/suggestion/review claims linked to that hypothesis are also
  excluded from that read model;
- new proactive recommendation/suggestion generation is blocked;
- bounded review fails closed while the privacy control is active;
- an already-delivered suggestion cannot become INTERESTED after privacy was
  applied.

Privacy does not mean deletion.

Canonical TimelineEvent evidence and MemoryClaim history remain intact.

## Non-actionable semantics

Owner non-actionable means:

- the context remains available as internal knowledge;
- the source hypothesis may continue to exist and be revised by evidence;
- proactive recommendation/suggestion builders must not use it;
- existing execution-aware recommendation lifecycle revalidation fails closed
  if such a controlled source is encountered.

Non-actionable does not itself hide the context from the owner.

## Reversibility

Controls are versioned.

A newer owner command supersedes the previous active control snapshot for the
same hypothesis.

Clearing privacy changes only the privacy dimension.

Allowing suggestions again changes only the actionability dimension.

Removing either restriction does not grant:

- automatic disclosure;
- execution authority;
- capability permission;
- approval;
- grant;
- provider access.

Normal disclosure and execution boundaries still apply independently.

## Revalidation

Owner controls are consulted at both build-time and synchronous boundary-time.

V1R applies the overlay to:

- Personal Context V0 read model;
- recurrence recommendation building;
- anomaly suggestion building;
- anomaly suggestion reconciliation;
- bounded review source validation;
- explicit suggestion reply source validation;
- executable recommendation source/lifecycle validation.

This prevents a race where a pre-existing proposal crosses a boundary after the
owner has changed policy but before the next worker tick.

## Historical preservation

Applying a control does not change the source claim status.

The source hypothesis remains historically visible in storage with its original
provenance, confidence, expiry and evidence references.

The policy itself is separate USER_DECLARED knowledge.

## Proof

Tests cover:

- closed deterministic control vocabulary;
- privacy creates USER_DECLARED owner policy without authority;
- private context disappears from Personal Context read snapshots;
- source sequence/anomaly/suggestion history remains stored;
- private context does not build new anomaly suggestions;
- privacy survives a newly persisted snapshot with the same hypothesis_id;
- clearing privacy restores read eligibility without disclosure authority;
- non-actionable context stays visible as knowledge;
- non-actionable context cannot build new suggestions;
- clearing non-actionable restores suggestion eligibility without execution
  authority;
- no ExecutionIntentRow, AgentExecutionIntentRow, ReminderRow or FactRow is
  created by control changes.

## Runtime state

No migration.

No deploy.

No feature flag is enabled by this change.
