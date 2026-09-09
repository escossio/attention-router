# ADR 0014: Recent History and Persistent Memory

## Status

Accepted.

## Context

Recent turns provide conversational continuity, while persistent memory is
longer-lived and subject to eligibility, provenance and privacy rules. The
repository documents these as separate memory concerns.

## Decision

Keep recent interaction history distinct from persistent memory. Context
assembly may use both, but each source retains its own semantics, provenance
and retention expectations.

## Consequences

Short-term context can be bounded without deleting durable facts. Memory
selection remains reviewable, and future changes can address retention without
changing conversation ordering.

## Alternatives considered

Using the entire transcript as memory was rejected for scale and privacy.
Flattening all memory into recent history was rejected because durable facts
would disappear when the context window is trimmed.
