# Integration Adapter Proof V0

This proof exercises the same versioned Integration Contract with two materially different inbound channels: the current normalized WhatsApp Web bridge shape and a provider-neutral email message shape.

## Goal

Prove that channel-specific payload handling can terminate before the Attention Router core. After normalization, both sources produce the same `InboundIntegrationEvent`; downstream code consumes the neutral contract and does not branch on provider implementation details.

## Trusted boundary

`tenant_id`, integration instance identity, and bound account identity are supplied by trusted runtime configuration/authentication context. Provider/public payloads are forbidden from asserting `tenant_id`. If a raw payload contains `tenant_id`, the reference adapters fail closed with `UNTRUSTED_TENANT_FIELD_FORBIDDEN`.

External actor and thread identifiers remain external references until tenant-scoped identity resolution. The canonical `EventEnvelope.actor_id` stays empty until a resolver supplies a canonical actor ID. External addresses/IDs are not copied into prompt-facing sanitized event metadata.

## WhatsApp proof

`WhatsAppNormalizedAdapter` accepts the normalized payload shape already produced by the local WWEBJS bridge: external event ID, external actor ID, message type, occurrence time, metadata and the existing idempotency identity. It emits `InboundIntegrationEvent` with source `channel.whatsapp`.

This PR does not replace or mutate the live WWEBJS transport. It proves that the current transport can be adapted at the contract boundary.

## Email proof

`EmailNormalizedAdapter` accepts a normalized email message containing provider message/thread IDs, sender, subject/body references and already-staged attachment metadata. It emits the same `InboundIntegrationEvent` contract with source `channel.email`.

Attachments are emitted as `ArtifactReceiptContract` values and bridge into the existing Artifact Plane. The adapter does not introduce another document store.

The email payload used here is provider-neutral on purpose. Gmail/Microsoft OAuth, webhook/polling runtime and provider-specific API calls remain outside this proof.

## Resulting architecture

Provider API / transport -> provider adapter -> Integration Contract -> identity resolution / Artifact Plane -> canonical Attention Router domains.

The core does not need `if whatsapp` or `if gmail` to interpret channel ingress.

## SDK consequence

The proof deliberately comes before SDK extraction. Once two different channel adapters survive the same contract boundary, the reusable mechanics can be extracted into SDKs without making a Python package the source of truth. The JSON Schema remains the language-neutral contract; future Python/TypeScript SDKs should implement validation, authentication helpers, idempotency, retries, artifact staging and observability around that contract.

## Non-goals

No live email account, OAuth, provider credentials, background polling, Gmail-specific code, Microsoft Graph-specific code, outbound email delivery, production transport replacement, embeddings or disclosure authority are added here.
