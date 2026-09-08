# Architecture and authority boundaries

Attention Router separates a conversational agent's interpretation from permission to execute effects.

## Conversation pipeline

An inbound becomes a persisted interaction under a tenant/contact identity. Effective text is either message text or a READY voice transcript, not a media path. A bounded AllowedAgentContext carries approved context to Andy. Structured output proposes a response/actions; policy, review and autonomy determine whether execution is permitted.

Authorized execution creates an intent and outbox work. Voice responses use a separately authenticated TTS service, then MP3-to-Ogg/Opus normalization before transport. Delivery evidence is distinct from generation, TTS readiness and enqueue success. A response that was merely proposed or ambiguously sent must not be asserted as delivered.

## Human authority and fail-closed execution

Owner pause/resume and explicit approvals are durable controls checked by the execution pipeline. The model cannot grant itself permission. Idempotency keys, transactional outbox and claim/lease mechanisms prevent duplicate processing; ambiguous external effects require human review rather than blind replay.

## Three context concepts

Recent history includes up to six earlier interactions, each with a user entry and at most one delivered assistant entry. Temporal cutoff prevents future interaction/response leakage during reprocessing. Unanswered human turns remain useful context.

Persistent memory is a separate subsystem with eligibility, sensitivity, provenance and disclosure controls. Conversation Session State is a future design, not an implemented third memory layer. Game state must not automatically become a permanent memory claim.

## Language and voice

The system resolves response locale from the supported deterministic signals and propagates it through context and structured execution metadata to the TTS request. Neutral text such as a number retains a language signal. This is not a universal language detector or a guarantee of provider pronunciation.

## Provenance and observability

Decision, authorization, intent, derivation, outbox and delivery evidence describe different stages. Trace context helps correlate them without logging private payloads or credentials. Build provenance should identify the actual deployed component, not assume that a branch name equals runtime state.

## Isolation

Tenant/contact boundaries apply to history and decisions; active context and memory disclosure add scope. Internal ingress authentication, data minimization and explicit external-service configuration are necessary even when local examples bind to loopback.
