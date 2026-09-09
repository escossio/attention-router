# ADR 0019: Transactional Outbox and Delivery State

## Status

Accepted.

## Context

The existing outbox ADR records atomic creation of work with a decision. The
public architecture also needs to explain why intent, processing and outcome
are separate.

## Decision

Persist the execution intent with the decision transaction, then process it
through explicit outbox states and record delivery evidence separately.

## Consequences

Retries and idempotency can be reasoned about without claiming delivery. A
worker or provider failure remains observable instead of being hidden inside a
request transaction.

## Alternatives considered

Calling providers inline during decision persistence was rejected because
network failure would couple external effects to the database transaction.

