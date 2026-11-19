"""Controlled public-style fixtures only: never reads production conversations."""

from datetime import datetime, timezone
import json
import time
import resource
from pathlib import Path
from attention_router.application.conversation_embedding import LocalEmbeddingProvider
from attention_router.application.context_retrieval import rank_personal_context
from attention_router.application.personal_context import (
    PersonalContextSnapshot,
    PersonalContextFact,
)

texts = [
    "A consulta com o dentista ficou marcada para quarta-feira.",
    "Vou levar o carro à oficina para trocar o óleo e revisar os freios.",
    "O boleto foi pago e o comprovante já está disponível.",
    "Vamos almoçar juntos amanhã ao meio-dia.",
    "O jardim precisa ser regado e as plantas podadas.",
    "O filme começa às oito da noite no cinema.",
]
queries = [
    "atendimento odontológico",
    "manutenção do veículo",
    "quitação da cobrança",
    "refeição na hora do almoço",
]
stamp = datetime.now(timezone.utc)
facts = tuple(
    PersonalContextFact(
        str(i),
        "conversation_text",
        {"text": t},
        None,
        "ASSERTED",
        "CONTROLLED_TEST",
        str(i),
        1,
        stamp,
        None,
        None,
    )
    for i, t in enumerate(texts)
)
snapshot = PersonalContextSnapshot("test", "test", 1, (), facts, stamp)
start = time.monotonic()
cpu = time.process_time()
provider = LocalEmbeddingProvider()
init = time.monotonic() - start
vectors = provider.encode(texts)
results = []
for i, q in enumerate(queries):
    started = time.monotonic()
    vector = provider.encode([q], query=True)[0]
    scores = [sum(a * b for a, b in zip(vector, v)) for v in vectors]
    ordered = sorted(range(len(scores)), key=lambda k: scores[k], reverse=True)
    lexical = rank_personal_context(snapshot, q, limit=6)
    lex_ids = [int(x.item_id) for x in lexical.items]
    results.append(
        {
            "case": i,
            "vector_rank": ordered.index(i) + 1,
            "lexical_rank": lex_ids.index(i) + 1 if i in lex_ids else None,
            "query_ms": round((time.monotonic() - started) * 1000, 2),
        }
    )
result = {
    "controlled_cases": results,
    "vector_top1": sum(r["vector_rank"] == 1 for r in results),
    "lexical_top1": sum(r["lexical_rank"] == 1 for r in results),
    "case_count": len(results),
    "model_init_seconds": init,
    "elapsed_seconds": time.monotonic() - start,
    "cpu_seconds": time.process_time() - cpu,
    "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    "limitations": "Small controlled set; not a production relevance benchmark.",
}
Path("/app/vector-quality.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result))
