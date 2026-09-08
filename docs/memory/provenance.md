# Provenance

Every promoted claim has one or more `memory_evidence` rows pointing to the archive message and extraction run. `get_memory_evidence(claim_id)` returns those references; the service never reconstructs provenance from free text. Operator corrections can later be represented as higher-quality claims while preserving the previous claim as history.
# Provenance incremental

Cada claim aponta para `memory_evidence`, que aponta para `conversation_messages` e `memory_extraction_runs`; a mensagem aponta para `conversation_threads` e para o actor observado. A cadeia responde quem declarou, quando, em qual mensagem/conversation e qual método/extraction run promoveu o fato.
