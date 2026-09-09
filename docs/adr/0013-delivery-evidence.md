# ADR 0013: Delivery Evidence

## Status

Accepted.

## Context

Creating a response or queueing an action does not prove that a provider
accepted or delivered it. This distinction is reflected in the current outbox
and observability concepts.

## Decision

Record proposal, authorization, execution state and delivery evidence as
distinct facts. A proposed response is never presented as delivered without
provider evidence.

## Consequences

Operators can distinguish pending, failed and confirmed work. Audits are more
useful, while integrations must provide honest status mapping and evidence.

## Alternatives considered

Treating queue insertion as delivery was rejected because it hides downstream
failure. A single free-form activity log was rejected because it loses state
boundaries and is harder to query reliably.
