# Schema V0

The additive migration `0010_memory_archive` creates:

- `memory_actors`
- `conversation_threads`
- `conversation_participants`
- `conversation_messages`
- `memory_extraction_runs`
- `memory_candidates`
- `memory_claims`
- `memory_evidence`
- `memory_ingestion_jobs`

Messages are idempotent on `(source, source_account, source_message_id)`. Threads are idempotent on `(source, source_account, external_thread_key)`. Claims preserve `status`, `valid_from`, `valid_until`, `supersedes_claim_id`, confidence, source quality, and staleness. All message text used for search is redacted before persistence when a high-confidence secret detector matches.

The archive supports `TEXT`, captions/descriptions, and metadata-only unsupported/media records. OCR and transcription are intentionally out of scope.
# Runtime incremental additions

- `conversation_threads`, `conversation_participants` e `conversation_messages`: archive canônico, com unicidade por `source/source_account/source_message_id`.
- `memory_ingestion_jobs`: uma linha lógica por mensagem, estados `PENDING`, `PROCESSING`, `DONE` ou `ERROR`.
- `memory_candidates`, `memory_claims` e `memory_evidence`: extração, promoção e provenance; retry reutiliza candidate/claim/evidence equivalentes.

O archive preserva `from_me`, `direction`, `observed_display_name` e `sensitivity_class`. Texto `SECRET` fica redigido e não pesquisável.
