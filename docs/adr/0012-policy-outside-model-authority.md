# ADR 0012: Policy Outside Model Authority

## Status

Accepted.

## Context

An agent proposal is untrusted output. Treating a model response as
permission would allow prompt content or model errors to cross directly into
side effects. This is an interpretation of the current policy and execution
boundaries, not a claim about the original historical motivation.

## Decision

The model may propose structured work, but policy evaluation owns the
authorization decision. Execution code must consume the resulting decision,
not infer authority from free-form model text.

## Consequences

Policy can be inspected and versioned independently from model prompts.
Provider changes do not silently expand autonomy. The system has an explicit
boundary to test before any external adapter is enabled.

## Alternatives considered

Allowing the model to invoke tools directly was rejected because it collapses
proposal and authorization. A single hard-coded global permission was rejected
because it cannot express per-action policy.
