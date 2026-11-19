"""Local textual support, not entailment, truth probability or action authority."""

import math
import re
from attention_router.application.context_retrieval import _normalize, _tokens, _candidate_score

POLICY_VERSION = "HYBRID_TEXTUAL_SUPPORT_V1"
# General language equivalences, declared before calibration; no identities or private examples.
CONCEPTS = {
    "audio": ("mensagem de voz", "gravacao sonora", "registro sonoro", "audios", "audio"),
    "transcricao": (
        "conversao de fala em texto",
        "fala convertida em texto",
        "transcricoes",
        "transcricao",
    ),
    "pix": ("transferencia instantanea", "transferencias instantaneas", "pix"),
    "pagamento": ("quitacao financeira", "pagamentos", "pagamento"),
    "contrato": ("acordo contratual", "instrumento contratual", "contratos", "contrato"),
    "projeto": ("projetos", "projeto"),
    "conversa": ("dialogos", "dialogo", "conversas", "conversa"),
    "horario": ("marcacao horaria", "horarios", "horario"),
    "problema": ("dificuldades", "dificuldade", "problemas", "problema"),
    "amanha": ("dia seguinte", "proximo dia", "amanha"),
    "hoje": ("dia corrente", "data corrente", "hoje"),
    "segundo": ("segundos", "segundo"),
    "minuto": ("minutos", "minuto"),
    "hora": ("horas", "hora"),
    "combinado": ("acordo expresso", "confirmacao de acordo", "combinado"),
}
GENERIC = set(
    "ha houve existe existem mencao mencoes referencia referencias mencionado mencionada mencionados mencionadas citado citada citados citadas aparece aparecem consta constam algum alguma alguma algo conteudo informacao informacoes registro registros mensagem mensagens trecho trechos procure localizar encontrar registrado registrada registrados registradas literalmente explicitamente indique mostre foi foram esta estao estava estavam ser sendo qual quais quero gostaria saber texto textos nessa nesse nesta neste nessa desta deste deles delas antes depois imediatamente pergunta perguntas resposta respostas sequencia anterior posterior seguinte enviada enviado ocorreu ocorre falou falar dito disse contem sobre esse essa isso isto aquilo ele ela la ai aqui nesse".split()
)
DEICTIC = re.compile(r"\b(isso|isto|aquilo|ele|ela|la|ai)\b")


def terms(value):
    value = re.sub(r"(?m)^Autor\s+\d+:\s*", "", value)
    value = _normalize(value)
    for key, phrases in CONCEPTS.items():
        for phrase in sorted(phrases, key=len, reverse=True):
            value = re.sub(r"\b" + re.escape(phrase) + r"\b", key, value)
    return _tokens(value) - GENERIC


def lexical_score(query, content):
    return _candidate_score(
        query_tokens=_tokens(query), predicate="", searchable_value=content, confidence=0
    )[0]


def features(query, content):
    wanted, present = terms(query), terms(content)
    hit = wanted & present
    numbers = {t for t in wanted if t.isdigit()}
    lookup_query = bool(
        re.search(r"\b(mencao|referencia|mencionad\w*|citad\w*)\b", _normalize(query))
    ) or "aparece imediatamente depois" in _normalize(query)
    question_only_support = False
    if not lookup_query and wanted and wanted <= present and "?" in content:
        pieces = re.split(r"(?m)^Autor\s+\d+:\s*|(?<=[.!?])\s+", content)
        question_only_support = not any(
            wanted <= terms(piece) and "?" not in piece for piece in pieces
        )
    normalized_query = _normalize(query)
    required = None
    if not lookup_query:
        if re.match(r"^(quem|onde)\b", normalized_query):
            required = "UNRESOLVED_IDENTITY_OR_LOCATION"
        elif re.search(r"\b(quando|que dia|qual data|que horas|qual horario)\b", normalized_query):
            required = "TIME"
        elif re.search(r"\b(quanto tempo|qual duracao)\b", normalized_query):
            required = "DURATION"
        elif re.search(r"\b(quanto|qual valor|qual preco)\b", normalized_query):
            required = "AMOUNT"
    patterns = {
        "TIME": r"\b(?:hoje|amanha|ontem|segunda|terca|quarta|quinta|sexta|sabado|domingo)\b|\b\d{1,2}(?:[:/]\d{1,2}|h\b)",
        "DURATION": r"\b\d+(?:[.,]\d+)?\s*(?:segundos?|minutos?|horas?|dias?)\b",
        "AMOUNT": r"r\$\s*\d|\b\d+(?:[.,]\d+)?\s*reais\b",
    }
    answer_type_supported = required is None
    if required in patterns:
        pieces = re.split(r"(?m)^Autor\s+\d+:\s*|(?<=[.!?])\s+", content)
        answer_type_supported = any(
            wanted <= terms(piece)
            and "?" not in piece
            and re.search(patterns[required], _normalize(piece))
            for piece in pieces
        )
    return {
        "answer_type_supported": answer_type_supported,
        "required_answer_type": required,
        "question_only_support": question_only_support,
        "lookup_query": lookup_query,
        "anchor_count": len(wanted),
        "matched_anchor_count": len(hit),
        "coverage": len(hit) / max(1, len(wanted)),
        "missing_query_value": bool(numbers - present),
        "lexical_score": lexical_score(query, content),
    }


def order_candidates(candidates, query, *, strategy, config):
    rows = [dict(r, **features(query, r["content_text"])) for r in candidates]
    by_vector = sorted(rows, key=lambda r: (-r["similarity"], r["id"]))
    by_lexical = sorted(rows, key=lambda r: (-r["lexical_score"], r["id"]))
    # Expanded lexical coverage is separately observable, never replaces raw vector similarity.
    by_support = sorted(rows, key=lambda r: (-r["coverage"], -r["lexical_score"], r["id"]))
    vr = {r["id"]: i + 1 for i, r in enumerate(by_vector)}
    lr = {r["id"]: i + 1 for i, r in enumerate(by_support)}
    k, weight = config["rrf_k"], config["vector_weight"]
    for r in rows:
        r["relevance_score"] = weight / (k + vr[r["id"]]) + (1 - weight) / (k + lr[r["id"]])
        r["vector_rank"] = vr[r["id"]]
        r["lexical_rank"] = lr[r["id"]]
    if strategy == "vector":
        return by_vector
    if strategy == "lexical":
        return [r for r in by_lexical if r["lexical_score"] > 0]
    return sorted(rows, key=lambda r: (-r["relevance_score"], -r["similarity"], r["id"]))


def evidence_reason(row, config):
    if not row["anchor_count"]:
        return "QUERY_UNDERSPECIFIED"
    if not row.get("answer_type_supported", True):
        return "ANSWER_TYPE_NOT_ESTABLISHED"
    if row.get("question_only_support") and not row.get("lookup_query"):
        return "QUESTION_WITHOUT_ASSERTED_ANSWER"
    if row["missing_query_value"]:
        return "QUERY_VALUE_NOT_SUPPORTED"
    if row["coverage"] < config["min_coverage"]:
        return "QUERY_COVERAGE_INSUFFICIENT"
    if row["matched_anchor_count"] < config["min_anchors"]:
        return "TEXTUAL_SUPPORT_INSUFFICIENT"
    if not math.isfinite(row["similarity"]) or row["similarity"] < config["min_similarity"]:
        return "SEMANTIC_SUPPORT_INSUFFICIENT"
    return None


def ambiguous(query, *, conversation_id=None, participant=None):
    return (
        bool(DEICTIC.search(_normalize(query)))
        and not (conversation_id or participant)
        and len(terms(query)) < 2
    )
