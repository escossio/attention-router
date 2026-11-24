from datetime import timedelta
import pytest
from sqlalchemy import text
from test_passive_vectors import drain, NOW

from attention_router.application import conversation_hybrid as h
from attention_router.application.conversation_relevance import (
    evidence_reason, features, order_candidates, prepare_candidate_features, terms,
)

pytest_plugins = ("test_passive_vectors",)


def test_frozen_policy_provenance():
    p = h.load_policy()
    assert p["vector_weight"] == 0.75 and p["rrf_k"] == 10 and p["min_coverage"] == 1
    assert len(p["calibration_reference_sha256"]) == 64 and p["trials"] == 360


def test_synonyms_and_insufficient_coverage():
    assert terms("Há menção a áudio?") == terms("Existe referência a gravação sonora?")
    row = features(
        "Há pagamento para um dragão em Netuno?", "Autor 1: O pagamento foi confirmado."
    ) | {"similarity": 0.99}
    assert evidence_reason(row, h.load_policy()) == "QUERY_COVERAGE_INSUFFICIENT"


@pytest.mark.parametrize(
    "query,content,reason",
    [
        (
            "O pagamento foi confirmado?",
            "Autor 1: O pagamento foi confirmado?",
            "QUESTION_WITHOUT_ASSERTED_ANSWER",
        ),
        (
            "Quando foi o pagamento?",
            "Autor 1: Pagamento confirmado.",
            "ANSWER_TYPE_NOT_ESTABLISHED",
        ),
        (
            "Quanto foi o pagamento?",
            "Autor 1: Pagamento confirmado.",
            "ANSWER_TYPE_NOT_ESTABLISHED",
        ),
        (
            "Quem confirmou o pagamento?",
            "Autor 1: Pagamento confirmado.",
            "ANSWER_TYPE_NOT_ESTABLISHED",
        ),
        (
            "Qual pagamento de 999999 foi registrado?",
            "Autor 1: Pagamento de 10 reais.",
            "QUERY_VALUE_NOT_SUPPORTED",
        ),
    ],
)
def test_not_question_matching_or_missing_answer_type(query, content, reason):
    assert (
        evidence_reason(features(query, content) | {"similarity": 0.99}, h.load_policy()) == reason
    )


@pytest.mark.parametrize("query", [
    "Há menção a uma gravação sonora?", "Qual foi a duração de 20 minutos?",
    "Quanto foi o pagamento?", "O pagamento foi confirmado?",
])
def test_prepared_scores_preserve_lexical_vector_and_rrf_order(query):
    candidates = [
        {"id": "a", "content_text": "Autor 1: O áudio durou 20 minutos.", "similarity": 0.84},
        {"id": "b", "content_text": "Autor 2: O pagamento foi confirmado?", "similarity": 0.67},
        {"id": "c", "content_text": "Autor 2: O pagamento de 35 reais foi confirmado.", "similarity": 0.67},
    ]
    prepared = prepare_candidate_features(query)
    for strategy in ("vector", "lexical", "hybrid"):
        args = dict(strategy=strategy, config=h.load_policy())
        assert order_candidates(candidates, query, prepared=prepared, **args) == order_candidates(candidates, query, **args)


@pytest.mark.postgres
def test_real_model_hybrid_negative_and_read_only(db, provider):
    f, tenant, conv, msg = db
    t = tenant()
    c = conv(t)
    mid, _ = msg(t, c, "Enviei um áudio com a confirmação.", out=True, voice=True)
    drain(f, provider)
    with f() as s:
        before = s.execute(
            text("SELECT count(*),max(created_at) FROM conversation_embeddings")
        ).one()
        stats = s.execute(
            text("SELECT name,value FROM conversation_vector_stats ORDER BY name")
        ).all()
        result = h.search(
            s,
            tenant_id=t,
            source_account="default",
            conversation_id=c,
            query="Há menção a áudio?",
            provider=provider,
        )
        assert result["state"] == "EVIDENCE_FOUND"
        assert any(
            m["id"] == mid and m["transcription_id"] and m["direction"] == "OUTBOUND"
            for r in result["results"]
            for m in r["messages"]
        )
        assert result["execution_authority"] is False
        other = h.search(
            s,
            tenant_id=t,
            source_account="default",
            conversation_id=c,
            query="Existe referência a gravação sonora?",
            provider=provider,
        )
        assert [r["segment_id"] for r in result["results"]] == [
            r["segment_id"] for r in other["results"]
        ]
        bad = h.search(
            s,
            tenant_id=t,
            source_account="default",
            query="Qual é o código secreto do dragão espacial em Netuno?",
            provider=provider,
        )
        assert bad["state"] == "INSUFFICIENT_EVIDENCE" and not bad["results"]
        assert (
            s.execute(text("SELECT count(*),max(created_at) FROM conversation_embeddings")).one()
            == before
        )
        assert (
            s.execute(text("SELECT name,value FROM conversation_vector_stats ORDER BY name")).all()
            == stats
        )
        assert s.execute(text("SELECT count(*) FROM outbox_messages")).scalar() == 0
        assert s.execute(text("SELECT count(*) FROM memory_claims")).scalar() == 0


@pytest.mark.postgres
def test_scope_filters_unknown_author_pending_voice(db, provider):
    f, tenant, conv, msg = db
    t = tenant()
    other = tenant()
    c = conv(t)
    foreign = conv(other)
    mid, _ = msg(t, c, "O áudio está disponível.", out=True)
    pending, _ = msg(t, c, "Áudio ainda pendente.", minute=2, voice=True, ready=False)
    msg(other, foreign, "O áudio de outro tenant.", out=True)
    drain(f, provider)
    with f() as s:
        result = h.search(
            s,
            tenant_id=t,
            source_account="default",
            query="Há menção a áudio?",
            direction="OUTBOUND",
            start_at=NOW,
            end_at=NOW,
            provider=provider,
        )
        assert result["results"]
        for r in result["results"]:
            assert r["conversation_id"] == c
            assert all(m["id"] != pending and m["sender_actor_id"] is None for m in r["messages"])
        assert not h.search(
            s,
            tenant_id=t,
            source_account="other-account",
            query="Há menção a áudio?",
            provider=provider,
        )["results"]
        assert not h.search(
            s,
            tenant_id=t,
            source_account="default",
            participant="unknown-actor",
            query="Há menção a áudio?",
            provider=provider,
        )["results"]
        assert (
            h.search(
                s,
                tenant_id=t,
                source_account="default",
                query="Como ficou isso?",
                provider=provider,
            )["state"]
            == "INSUFFICIENT_EVIDENCE"
        )
        with pytest.raises(PermissionError):
            h.search(
                s, tenant_id="absent", source_account="default", query="áudio", provider=provider
            )


@pytest.mark.parametrize("change", ["delete", "update", "revoke", "metadata"])
@pytest.mark.postgres
def test_change_after_selection_is_not_returned(db, provider, monkeypatch, change):
    f, tenant, conv, msg = db
    t = tenant()
    c = conv(t)
    mid, _ = msg(t, c, "O áudio foi enviado.")
    drain(f, provider)
    original = h.v.search

    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        with f() as other:
            if change == "delete":
                other.execute(text("DELETE FROM conversation_messages WHERE id=:id"), {"id": mid})
            elif change == "update":
                other.execute(
                    text(
                        "UPDATE conversation_messages SET text='Conteúdo corrigido.' WHERE id=:id"
                    ),
                    {"id": mid},
                )
            elif change == "metadata":
                other.execute(
                    text("UPDATE conversation_messages SET sent_at=sent_at+interval '1 second' WHERE id=:id"),
                    {"id": mid},
                )
            else:
                other.execute(
                    text(
                        "UPDATE client_tenant_memberships SET status='REVOKED' WHERE tenant_id=:t"
                    ),
                    {"t": t},
                )
            other.commit()
        if change == "metadata":
            drain(f, provider)
        return result

    monkeypatch.setattr(h.v, "search", race)
    with f() as s:
        if change == "revoke":
            with pytest.raises(PermissionError):
                h.search(
                    s,
                    tenant_id=t,
                    source_account="default",
                    query="Há menção a áudio?",
                    provider=provider,
                )
        else:
            r = h.search(
                s,
                tenant_id=t,
                source_account="default",
                query="Há menção a áudio?",
                provider=provider,
            )
            assert r["state"] == "INSUFFICIENT_EVIDENCE" and not r["results"]


@pytest.mark.postgres
def test_bounded_context_with_short_answer_no_invented_reply(db, provider):
    f, tenant, conv, msg = db
    t = tenant()
    c = conv(t)
    q, _ = msg(t, c, "Você recebeu o áudio?", minute=0)
    a, _ = msg(t, c, "Sim.", minute=1, out=True)
    msg(t, c, "Outro assunto distante.", minute=40)
    drain(f, provider)
    with f() as s:
        result = h.search(
            s,
            tenant_id=t,
            source_account="default",
            conversation_id=c,
            query="Há menção a áudio?",
            provider=provider,
        )
        ids = {m["id"] for r in result["results"] for m in r["messages"]}
        assert {q, a} <= ids
        assert all(
            r["context_expansion"]["added_messages"] <= 2
            and not r["context_expansion"]["relation_inferred"]
            for r in result["results"]
        )
        assert all(
            m["sent_at"] <= NOW + timedelta(minutes=1)
            for r in result["results"]
            for m in r["messages"]
        )


@pytest.mark.postgres
def test_incremental_stt_still_indexes_without_search_writes(db, provider):
    f, tenant, conv, msg = db
    t = tenant()
    c = conv(t)
    mid, eid = msg(t, c, "Áudio novo.", voice=True, ready=False)
    drain(f, provider)
    with f() as s:
        assert not h.search(
            s, tenant_id=t, source_account="default", query="Há menção a áudio?", provider=provider
        )["results"]
        s.execute(
            text(
                "UPDATE voice_transcriptions SET status='READY',transcript_text='Este áudio chegou agora.' WHERE inbound_event_id=:e"
            ),
            {"e": eid},
        )
        s.commit()
    drain(f, provider)
    with f() as s:
        r = h.search(
            s, tenant_id=t, source_account="default", query="Há menção a áudio?", provider=provider
        )
        assert any(m["id"] == mid for hit in r["results"] for m in hit["messages"])


@pytest.mark.postgres
def test_large_neighbor_is_bounded_and_offsets_verifiable(db, provider):
    f, tenant, conv, msg = db
    t = tenant()
    c = conv(t)
    body = "Detalhe " * 1000 + "Pergunta final?"
    previous, _ = msg(t, c, body)
    current, _ = msg(t, c, "Sim.", minute=1, out=True)
    with f() as s:
        result = h._context(
            s,
            {
                "conversation_id": c,
                "messages": [
                    {
                        "id": current,
                        "text": "Sim.",
                        "sent_at": NOW + timedelta(minutes=1),
                        "char_start": 0,
                    }
                ],
            },
            t,
            "default",
            provider,
            {r["id"]: h.v._source_version(r) for r in h.v.sources(s, t, "default", c)},
        )
        neighbor = next(m for m in result["messages"] if m["id"] == previous)
        assert neighbor["context_truncated"] and neighbor["char_start"] > 0
        assert provider.count(neighbor["text"]) <= 350
        assert neighbor["text"] == body[neighbor["char_start"] : neighbor["char_end"]]
        assert neighbor["role"] == "CHRONOLOGICAL_CONTEXT_NOT_CONFIRMED_REPLY"


@pytest.mark.parametrize(("query", "content", "reason"), [
    ("Há menção à sala Marfim na reserva de cerâmica?",
     "A equipe de cerâmica reservou a sala Marfim.", None),
    ("Qual a duração do ensaio do quarteto de cordas?",
     "O ensaio do quarteto de cordas durou 45 minutos.", None),
    ("Qual a duração do ensaio do quarteto de cordas?",
     "O ensaio do quarteto de cordas durou muito.", "ANSWER_TYPE_NOT_ESTABLISHED"),
    ("Quando terminou o inventário do viveiro de lírios?",
     "O inventário do viveiro de lírios terminou em março.", None),
    ("Quando terminou o inventário do viveiro de lírios?",
     "O inventário do viveiro de lírios terminou em um marco importante.",
     "ANSWER_TYPE_NOT_ESTABLISHED"),
    ("Que horas abre a biblioteca do farol?",
     "A biblioteca do farol abre às nove horas.", None),
    ("Que horas abre a biblioteca do farol?",
     "A biblioteca do farol abre por nove horas.", "ANSWER_TYPE_NOT_ESTABLISHED"),
])
def test_grounded_portuguese_lookup_forms(query, content, reason):
    for prepared in (None, prepare_candidate_features(query)):
        row = features(query, content, prepared=prepared) | {"similarity": 0.95}
        assert evidence_reason(row, h.load_policy()) == reason
