# ADR 0016: External TTS Service Boundary

## Status

Accepted.

## Context

Text-to-speech has provider-specific credentials, latency and failure modes.
Current voice documentation treats it as an adapter/service concern.

## Decision

Keep TTS behind a versioned external service boundary and adapter contract;
the core reasons over text and action state, not provider internals.

## Consequences

Providers can be replaced or disabled without changing core decision logic.
Availability, cost, data handling and output compatibility remain integration
responsibilities.

## Alternatives considered

Embedding one TTS SDK in the core was rejected because it couples the runtime
to one provider. Making voice a core invariant was rejected because text-only
operation remains valid.
