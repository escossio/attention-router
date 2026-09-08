# Search

Private search first queries structured current and historical claims, then the local archive. Results include source type, actor, conversation/message reference, observed time, confidence, and claim status. The current provider uses PostgreSQL-compatible indexed columns and a portable `ilike`/fuzzy fallback for SQLite tests; PostgreSQL full-text/trigram indexes can be added without introducing a vector dependency.

Private endpoints are authenticated under `/api/v1/private/memory/*`. There is no public conversational memory endpoint. Unknown facts remain unknown.
# Retrieval boundary

`memory actor`, `memory search`, `memory evidence` e `memory stats` são APIs/CLI privadas de operador. `memory_context(actor_id)` retorna somente claims ativas, actor-scoped, com limite de contexto; claims superseded só entram em consultas históricas. Contexto persistente é fail-closed e separado de capture por flags.
