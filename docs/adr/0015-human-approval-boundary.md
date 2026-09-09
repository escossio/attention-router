# ADR 0015: Human Approval Boundary

## Status

Accepted.

## Context

Some actions have consequences that policy alone should not silently approve.
The public architecture and deterministic demo therefore model approval as a
separate control point.

## Decision

Actions classified as requiring approval pause at a human approval boundary.
Only an explicit approval can release them to execution; model output cannot
stand in for that approval.

## Consequences

Critical actions gain a visible intervention point and an auditable decision.
The tradeoff is latency and the need to handle pending or rejected work.

## Alternatives considered

Fully autonomous execution was rejected for critical actions. Approval inside
the prompt was rejected because it is not an independent authority boundary.
