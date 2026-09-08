# Persistent actor memory and conversation archive

Phase 5.1 adds two separate read models. `conversation_messages` is the searchable archive and may contain `ARCHIVE_ONLY` facts. `memory_claims` contains only eligible, promoted facts. Archive import never calls the live ingress service and never creates `inbound_events`, decisions, reviews, execution intents, or outbox rows.

The existing `actor_bindings` remains the external identity boundary. `memory_actors` gives the archive a stable internal actor reference using the existing `actor_key`; display names remain observations, not identity.

Incremental extraction is asynchronous and non-blocking. The current provider is deterministic V0 behind the extraction service boundary; a future AI provider must return validated structured candidates and may not block delivery.

Behavior receives a small `memory_context` contract, not the database. Memory availability is not permission to disclose it.

The history adapter is a separate read-only boundary. It is fail-closed by default, HMAC path-bound, bounded to 100 messages per request, and never invokes send/read-state/presence/browser-lifecycle APIs.
# Incremental persistent actor memory

Mensagens novas entram pelo mesmo `receive_normalized_inbound_event()` usado pelo ingress. Depois da receipt/interaction canônica, o side-channel cria uma linha idempotente em `conversation_messages`; a extração não roda nessa transação de decisão.

`memory_ingestion_jobs` é processada pelo worker depois do pipeline normal. Falha de archive/extractor é auditada e não altera decisão, autonomia, transporte ou outbound. Mensagens `from_me` podem ser arquivadas, mas terminam sem interaction/Decision e nunca retornam ao Decision Engine.

O retrieval é actor-scoped e limitado a claims ativas/relevantes. `preferred_name` e `self_reported_name` não substituem display name. Claims têm evidence ligada à mensagem, extraction run e conversation.
