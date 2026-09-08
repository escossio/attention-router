# Privacy and sensitivity

Sensitivity is intentionally small: `NORMAL`, `PERSONAL`, `SENSITIVE`, `SECRET`. Secret content is redacted before archive persistence and excluded from general search. Sensitive content remains local but requires explicit relevance rules for promotion. Actor isolation is enforced by actor-scoped retrieval; one actor's context must never be injected into another actor's conversation.

Memory retrieval is contextual assistance, not automatic disclosure. Forget/delete operations are a future audited tombstone workflow and are not run automatically by ingestion.
