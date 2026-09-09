# ADR 0017: WhatsApp as an Adapter

## Status

Accepted.

## Context

The repository has transport-specific adapters and a provider-agnostic core.
WhatsApp is one ingress/egress integration and must not define the internal
architecture.

## Decision

Keep WhatsApp at the adapter boundary. Normalize inbound events before core
processing and keep provider payloads out of core contracts.

## Consequences

Other transports can reuse policy, memory, execution and evidence concepts.
WhatsApp-specific availability, authentication and payload behavior remain
integration concerns.

## Alternatives considered

Making WhatsApp message objects the core event model was rejected because it
would spread provider coupling throughout the system.
