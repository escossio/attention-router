from datetime import datetime, timezone, timedelta
import os
import uuid
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from attention_router.application import conversation_vectors as v
from attention_router.application.conversation_embedding import LocalEmbeddingProvider
from attention_router.application.conversation_segments import segment_messages
from attention_router.infrastructure.models import (
    TenantRow,
    ConversationThreadRow,
    ConversationMessageRow,
    InboundEventRow,
    VoiceTranscriptionRow,
)
from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.client_bootstrap_models import ClientTenantMembershipRow

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc)


def uid():
    return str(uuid.uuid4())


@pytest.fixture(scope="module")
def provider():
    return LocalEmbeddingProvider()


@pytest.fixture
def db():
    engine = create_engine(os.environ["PUBLIC_POSTGRES_TEST_URL"])
    factory = sessionmaker(engine, expire_on_commit=False)
    tenants = []

    def tenant():
        t = uid()
        h = uid()
        with factory() as s:
            s.add(
                TenantRow(
                    id=t,
                    slug=t,
                    name="Synthetic vector test",
                    status="ACTIVE",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            s.add(HumanIdentityRow(id=h, created_at=NOW))
            s.commit()
            s.add(
                ClientTenantMembershipRow(
                    id=uid(),
                    human_identity_id=h,
                    tenant_id=t,
                    role="OWNER",
                    status="ACTIVE",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            s.commit()
        tenants.append((t, h))
        return t

    def conversation(t, account="default"):
        c = uid()
        with factory() as s:
            s.add(
                ConversationThreadRow(
                    id=c,
                    tenant_id=t,
                    source="wwebjs",
                    source_account=account,
                    external_thread_key=uid() + "@lid",
                    thread_type="DIRECT",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            s.commit()
        return c

    def message(
        t, c, body, minute=0, out=False, account="default", reply=None, voice=False, ready=True
    ):
        mid = uid()
        eid = uid()
        with factory() as s:
            if voice:
                s.add(
                    InboundEventRow(
                        id=eid,
                        tenant_id=t,
                        source="wwebjs",
                        external_event_id=eid,
                        event_type="message",
                        payload={},
                        payload_hash="synthetic",
                        received_at=NOW,
                        status="processed",
                        correlation_id=uid(),
                    )
                )
                s.commit()
                s.add(
                    VoiceTranscriptionRow(
                        id=uid(),
                        tenant_id=t,
                        inbound_event_id=eid,
                        status="READY" if ready else "PENDING",
                        transcript_text=body if ready else None,
                        attempt_count=1,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
            s.add(
                ConversationMessageRow(
                    id=mid,
                    tenant_id=t,
                    conversation_id=c,
                    source="wwebjs",
                    source_account=account,
                    source_message_id=mid,
                    external_sender_key="owner" if out else "peer",
                    direction="OUTBOUND" if out else "INBOUND",
                    from_me=out,
                    sent_at=NOW + timedelta(minutes=minute),
                    message_type="VOICE" if voice else "TEXT",
                    text=body if ready else None,
                    content_hash=v.digest(body),
                    sensitivity_class="NORMAL",
                    searchable=True,
                    metadata_json={"source_inbound_event_id": eid} if voice else {},
                    reply_to_message_id=reply,
                    imported_at=NOW,
                    created_at=NOW,
                )
            )
            s.commit()
        return mid, eid

    yield factory, tenant, conversation, message
    with factory() as s:
        for t, h in tenants:
            s.execute(text("DELETE FROM conversation_messages WHERE tenant_id=:t"), {"t": t})
            s.execute(text("DELETE FROM voice_transcriptions WHERE tenant_id=:t"), {"t": t})
            s.execute(text("DELETE FROM inbound_events WHERE tenant_id=:t"), {"t": t})
            s.execute(text("DELETE FROM conversation_threads WHERE tenant_id=:t"), {"t": t})
            s.execute(
                text("DELETE FROM queue WHERE kind=:k AND payload->>'tenant_id'=:t"),
                {"k": v.KIND, "t": t},
            )
            s.execute(text("DELETE FROM client_tenant_memberships WHERE tenant_id=:t"), {"t": t})
            s.execute(text("DELETE FROM human_identities WHERE id=:h"), {"h": h})
            s.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": t})
        s.commit()
    engine.dispose()


def drain(factory, provider):
    for _ in range(30):
        with factory() as s:
            s.execute(
                text(
                    "UPDATE queue SET payload=jsonb_set(payload,'{next_at}','0') WHERE kind=:k AND status='PENDING'"
                ),
                {"k": v.KIND},
            )
            s.commit()
            count = s.execute(
                text(
                    "SELECT count(*) FROM queue WHERE kind=:k AND status IN ('PENDING','PROCESSING')"
                ),
                {"k": v.KIND},
            ).scalar()
        if not count:
            return
        v.cycle(factory, provider)
    pytest.fail("queue failed to drain")


def test_real_embeddings_portuguese(provider):
    import numpy as np

    vectors = np.array(
        provider.encode(
            ["A consulta com o dentista está marcada.", "O automóvel precisa trocar o óleo."]
        )
    )
    query = np.array(provider.encode(["atendimento odontológico"], query=True))[0]
    assert vectors.shape == (2, 384)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-5)
    assert (vectors @ query).argmax() == 0
    assert np.allclose(
        vectors,
        provider.encode(
            ["A consulta com o dentista está marcada.", "O automóvel precisa trocar o óleo."]
        ),
        atol=1e-5,
    )


def test_context_bilateral_short_reply_and_search(db, provider):
    f, tenant, conv, msg = db
    t = tenant()
    c = conv(t)
    a, _ = msg(t, c, "Podemos agendar a revisão do carro para amanhã?")
    b, _ = msg(t, c, "Sim, às nove.", minute=1, out=True, reply=a)
    drain(f, provider)
    with f() as s:
        r = v.search(
            s,
            tenant_id=t,
            source_account="default",
            query="manutenção do veículo",
            provider=provider,
        )
        assert len(r["results"]) == 1
        assert {m["id"] for m in r["results"][0]["messages"]} == {a, b}
        assert {m["direction"] for m in r["results"][0]["messages"]} == {"INBOUND", "OUTBOUND"}
        assert r["results"][0]["messages"][1]["reply_to_message_id"] == a
        assert r["results"][0]["trust"].startswith("UNTRUSTED")
        before = s.execute(text("select count(*) from conversation_embeddings")).scalar()
    drain(f, provider)
    with f() as s:
        assert s.execute(text("select count(*) from conversation_embeddings")).scalar() == before


def test_gap_long_text_explicit_reply_and_scope(db, provider):
    f, tenant, conv, msg = db
    t = tenant()
    c = conv(t)
    a, _ = msg(t, c, "A entrega será feita na próxima terça-feira.")
    b, _ = msg(t, c, "Pode confirmar.", minute=80, out=True, reply=a)
    msg(t, c, "Uma explicação sobre o projeto e suas etapas. " * 160, minute=150)
    with f() as s:
        rows = v.sources(s, t, "default", c)
    segments = segment_messages(rows, provider)
    assert len(segments) > 3
    assert any(m["role"] == "EXPLICIT_REPLY" for p in segments for m in p["members"])
    assert all(provider.count(p["text"]) <= 480 for p in segments)
    foreign = {**rows[0], "conversation_id": "other"}
    with pytest.raises(ValueError, match="SCOPE"):
        segment_messages(rows + [foreign], provider)
    assert sum(m["message"]["id"] == b for p in segments for m in p["members"]) == 1


@pytest.mark.parametrize("out", [False, True])
def test_voice_ready_incremental_and_correction(db, provider, out):
    f, tenant, conv, msg = db
    t = tenant()
    c = conv(t)
    mid, eid = msg(t, c, "A reunião será na quarta-feira.", voice=True, out=out, ready=False)
    drain(f, provider)
    with f() as s:
        assert not v.search(
            s, tenant_id=t, source_account="default", query="reunião", provider=provider
        )["results"]
        s.execute(
            text(
                "UPDATE voice_transcriptions SET status='READY',transcript_text='A reunião será na quarta-feira.' WHERE inbound_event_id=:e"
            ),
            {"e": eid},
        )
        s.commit()
    drain(f, provider)
    with f() as s:
        first = v.search(
            s, tenant_id=t, source_account="default", query="encontro na quarta", provider=provider
        )["results"][0]
        assert first["messages"][0]["id"] == mid
        s.execute(
            text(
                "UPDATE voice_transcriptions SET transcript_text='A reunião mudou para sexta-feira.' WHERE inbound_event_id=:e"
            ),
            {"e": eid},
        )
        s.commit()
        assert not v.search(
            s, tenant_id=t, source_account="default", query="reunião", provider=provider
        )["results"]
    drain(f, provider)
    with f() as s:
        new = v.search(
            s, tenant_id=t, source_account="default", query="encontro na sexta", provider=provider
        )["results"][0]
        assert new["segment_id"] == first["segment_id"]
        assert new["content_hash"] != first["content_hash"]
        assert new["messages"][0]["from_me"] == out
        assert (
            s.execute(
                text("select count(*) from conversation_embeddings where tenant_id=:t"), {"t": t}
            ).scalar()
            == 1
        )


def test_filters_tenant_account_participant_and_time(db, provider):
    f, tenant, conv, msg = db
    t = tenant()
    other = tenant()
    c = conv(t)
    c2 = conv(t, "second")
    c3 = conv(other)
    mid, _ = msg(t, c, "O pagamento da fatura está confirmado.", out=True)
    msg(t, c2, "O pagamento da fatura está confirmado.", account="second")
    msg(other, c3, "O pagamento da fatura está confirmado.")
    drain(f, provider)
    with f() as s:
        r = v.search(
            s,
            tenant_id=t,
            source_account="default",
            query="quitação",
            conversation_id=c,
            participant="owner",
            direction="OUTBOUND",
            start_at=NOW,
            end_at=NOW,
            provider=provider,
        )
        assert [m["id"] for x in r["results"] for m in x["messages"]] == [mid]
        for kw in [
            {"conversation_id": c2},
            {"participant": "unknown"},
            {"direction": "INBOUND"},
            {"start_at": NOW + timedelta(days=1)},
        ]:
            assert not v.search(
                s, tenant_id=t, source_account="default", query="quitação", provider=provider, **kw
            )["results"]


@pytest.mark.parametrize("action", ["hidden", "deleted", "revoked", "secret"])
def test_atomic_invalidation_and_erasure(db, provider, action):
    f, tenant, conv, msg = db
    t = tenant()
    c = conv(t)
    mid, _ = msg(t, c, "Vamos almoçar juntos amanhã.")
    drain(f, provider)
    with f() as s:
        if action == "hidden":
            s.execute(
                text("UPDATE conversation_messages SET searchable=false WHERE id=:id"), {"id": mid}
            )
        elif action == "deleted":
            s.execute(text("DELETE FROM conversation_messages WHERE id=:id"), {"id": mid})
        elif action == "secret":
            s.execute(
                text("UPDATE conversation_messages SET sensitivity_class='SECRET' WHERE id=:id"),
                {"id": mid},
            )
        else:
            s.execute(
                text("UPDATE client_tenant_memberships SET status='REVOKED' WHERE tenant_id=:t"),
                {"t": t},
            )
        s.commit()
        assert (
            s.execute(
                text("SELECT count(*) FROM conversation_embeddings WHERE tenant_id=:t"), {"t": t}
            ).scalar()
            == 0
        )
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM conversation_vector_segments WHERE tenant_id=:t AND content_text<>''"
                ),
                {"t": t},
            ).scalar()
            == 0
        )
        if action == "revoked":
            with pytest.raises(PermissionError):
                v.search(
                    s, tenant_id=t, source_account="default", query="almoço", provider=provider
                )
        else:
            assert not v.search(
                s, tenant_id=t, source_account="default", query="almoço", provider=provider
            )["results"]
    drain(f, provider)


def test_resume_and_generation_race(db, provider):
    f, tenant, conv, msg = db
    t = tenant()
    c = conv(t)
    mid, _ = msg(t, c, "O compromisso está confirmado.")

    class Racing:
        def __getattr__(self, n):
            return getattr(provider, n)

        def encode(self, texts, **kw):
            with f() as s:
                s.execute(
                    text(
                        "UPDATE conversation_messages SET text='O compromisso foi cancelado.' WHERE id=:id"
                    ),
                    {"id": mid},
                )
                s.commit()
            return provider.encode(texts, **kw)

    assert v.cycle(f, Racing()) == 0
    with f() as s:
        assert (
            s.execute(
                text("SELECT count(*) FROM conversation_embeddings WHERE tenant_id=:t"), {"t": t}
            ).scalar()
            == 0
        )
        s.execute(
            text(
                "UPDATE queue SET status='PROCESSING',payload=payload||jsonb_build_object('lease_until',0) WHERE id=:id"
            ),
            {"id": "vector:" + c},
        )
        s.commit()
    drain(f, provider)
    with f() as s:
        assert v.search(
            s, tenant_id=t, source_account="default", query="cancelamento", provider=provider
        )["results"]


def test_contact_metadata_does_not_reembed(db, provider):
    f, tenant, conv, msg = db
    t = tenant()
    c = conv(t)
    mid, _ = msg(t, c, "A entrega está confirmada.")
    drain(f, provider)
    with f() as s:
        before = s.execute(
            text("SELECT created_at FROM conversation_embeddings WHERE tenant_id=:t"), {"t": t}
        ).scalar()
        s.execute(
            text(
                'UPDATE conversation_messages SET metadata=metadata::jsonb||\'{"display_name":"Changed synthetic label"}\'::jsonb WHERE id=:id'
            ),
            {"id": mid},
        )
        s.commit()
    drain(f, provider)
    with f() as s:
        assert (
            s.execute(
                text("SELECT created_at FROM conversation_embeddings WHERE tenant_id=:t"), {"t": t}
            ).scalar()
            == before
        )


def test_row_level_security(db, provider):
    f, tenant, conv, msg = db
    t = tenant()
    c = conv(t)
    msg(t, c, "Informação exclusiva deste tenant.")
    drain(f, provider)
    with f() as s:
        s.execute(text("CREATE ROLE vector_rls_probe NOLOGIN"))
        s.execute(
            text(
                "GRANT SELECT ON conversation_vector_segments,conversation_embeddings,conversation_vector_memberships TO vector_rls_probe"
            )
        )
        s.execute(text("SET LOCAL ROLE vector_rls_probe"))
        v.scope(s, "another-tenant", "default")
        assert s.execute(text("SELECT count(*) FROM conversation_embeddings")).scalar() == 0
        v.scope(s, t, "default")
        assert s.execute(text("SELECT count(*) FROM conversation_embeddings")).scalar() == 1
        s.rollback()


def test_api_auth_is_fail_closed(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from attention_router.infrastructure.conversation_vector_runtime import install
    from fastapi import HTTPException

    app = FastAPI()

    def auth(value):
        if value != "Bearer synthetic-only":
            raise HTTPException(401)

    install(
        app,
        SimpleNamespace(admin_auth_enabled=True, admin_token="synthetic-only"),
        lambda: None,
        auth,
    )
    client = TestClient(app)
    assert (
        client.post(
            "/api/v1/private/conversation-search",
            json={"tenant_id": "test", "source_account": "default", "query": "teste"},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/v1/private/conversation-index/metrics?tenant_id=test&source_account=default"
        ).status_code
        == 401
    )


def test_bounded_failures_never_log_content(db, provider):
    f, tenant, conv, msg = db
    t = tenant()
    c = conv(t)
    msg(t, c, "Conteúdo controlado de teste.")

    class Broken:
        def __getattr__(self, n):
            return getattr(provider, n)

        def encode(self, *args, **kwargs):
            raise RuntimeError("PRIVATE_CONTENT_MUST_NOT_BE_STORED")

    for _ in range(5):
        with f() as s:
            s.execute(
                text("UPDATE queue SET payload=jsonb_set(payload,'{next_at}','0') WHERE id=:id"),
                {"id": "vector:" + c},
            )
            s.commit()
        with pytest.raises(RuntimeError):
            v.cycle(f, Broken())
    with f() as s:
        row = s.execute(
            text("SELECT status,payload FROM queue WHERE id=:id"), {"id": "vector:" + c}
        ).one()
        assert row.status == "FAILED" and row.payload["error"] == "RuntimeError"
        assert "PRIVATE_CONTENT" not in str(row.payload)
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM conversation_vector_segments WHERE tenant_id=:t AND state='FAILED'"
                ),
                {"t": t},
            ).scalar()
            == 1
        )


def test_restricted_service_role_indexes_and_searches(db, provider):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    f, tenant, conv, msg = db
    t = tenant()
    c = conv(t)
    msg(t, c, "Reunião sobre o orçamento na próxima semana.")
    role = "vector_service_probe"
    with f() as s:
        s.execute(text(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS"))
        s.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        if s.scalar(text("SELECT to_regprocedure('public.andy_context_index_writer_lock(text)') IS NOT NULL")):
            s.execute(text(f"GRANT EXECUTE ON FUNCTION public.andy_context_index_writer_lock(text) TO {role}"))
        s.execute(text(f"GRANT SELECT ON conversation_threads,conversation_messages,voice_transcriptions,client_tenant_memberships,queue TO {role}"))
        s.execute(text(f"GRANT UPDATE(status,payload,processed_at) ON queue TO {role}"))
        s.execute(text(f"GRANT SELECT,INSERT,UPDATE,DELETE ON conversation_vector_segments,conversation_vector_memberships,conversation_embeddings,conversation_vector_stats TO {role}"))
        s.commit()

    class RestrictedSession(Session):
        pass

    @event.listens_for(RestrictedSession, "after_begin")
    def set_role(session, transaction, connection):
        connection.exec_driver_sql(f"SET LOCAL ROLE {role}")

    restricted = sessionmaker(bind=f.kw["bind"], class_=RestrictedSession, expire_on_commit=False)
    try:
        drain(restricted, provider)
        with restricted() as s:
            result = v.search(s,tenant_id=t,source_account="default",query="planejar as despesas",provider=provider)
            assert len(result["results"]) == 1
            assert not s.execute(text("SELECT has_table_privilege(current_user,'conversation_messages','UPDATE')")).scalar()
            v.scope(s,"another-tenant","default")
            assert s.execute(text("SELECT count(*) FROM conversation_embeddings")).scalar() == 0
    finally:
        with f() as s:
            s.execute(text(f"DROP OWNED BY {role}"))
            s.execute(text(f"DROP ROLE {role}"))
            s.commit()


def test_owner_reassignment_erases_previous_scope_derivatives(db, provider):
    f, tenant, conv, msg = db
    t, other = tenant(), tenant()
    c = conv(t)
    msg(t,c,"Conteúdo privado do escopo anterior.")
    drain(f,provider)
    with f() as s:
        s.execute(text("UPDATE client_tenant_memberships SET tenant_id=:other WHERE tenant_id=:t"),{'other':other,'t':t})
        assert s.execute(text("SELECT count(*) FROM conversation_embeddings WHERE tenant_id=:t"),{'t':t}).scalar()==0
        assert s.execute(text("SELECT count(*) FROM conversation_vector_segments WHERE tenant_id=:t AND content_text<>''"),{'t':t}).scalar()==0
        s.rollback()


def test_index_lock_contention_does_not_abort_source_writer(db, provider):
    import threading
    from sqlalchemy import event

    f, tenant, conv, msg = db
    t = tenant()
    c = conv(t)
    mid, _ = msg(t,c,"Primeira versão do compromisso.")
    reached = threading.Event()
    failures = []
    engine = f.kw['bind']

    def before_execute(conn,cursor,statement,parameters,context,executemany):
        if 'INSERT INTO conversation_vector_memberships' in statement:
            reached.set()

    def worker():
        try:
            v.cycle(f,provider)
        except Exception as exc:
            failures.append(type(exc).__name__)

    event.listen(engine,'before_cursor_execute',before_execute)
    try:
        with f() as s:
            s.execute(text("SET LOCAL statement_timeout='5s'"))
            s.execute(text('SELECT id FROM conversation_messages WHERE id=:m FOR UPDATE'),{'m':mid})
            thread=threading.Thread(target=worker)
            thread.start()
            assert reached.wait(5)
            s.execute(text('UPDATE conversation_messages SET text=:body WHERE id=:m'),{'m':mid,'body':'Compromisso corrigido e confirmado.'})
            s.commit()
        thread.join(5)
        assert not thread.is_alive()
        assert failures==['OperationalError']
        drain(f,provider)
        with f() as s:
            result=v.search(s,tenant_id=t,source_account='default',query='compromisso',provider=provider)
            assert 'corrigido' in result['results'][0]['text']
    finally:
        event.remove(engine,'before_cursor_execute',before_execute)


@pytest.fixture
def vector_reader(db):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from attention_router.infrastructure.vector_attachment_provenance import CREATE_VECTOR_ATTACHMENT_PROVENANCE

    factory = db[0]
    role = "vector_reader_" + uuid.uuid4().hex
    with factory.begin() as admin:
        install = admin.scalar(text("SELECT to_regclass('public.andy_vector_attachment_provenance') IS NULL"))
        if install:
            # U2 can exercise the actual G3 projection on a disposable pre-0072 database.
            admin.execute(text(CREATE_VECTOR_ATTACHMENT_PROVENANCE))
            admin.execute(text("REVOKE ALL ON public.andy_vector_attachment_provenance FROM PUBLIC"))
        admin.execute(text(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS"))
        admin.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        admin.execute(text(f"""GRANT SELECT ON conversation_threads, conversation_messages,
            voice_transcriptions, client_tenant_memberships, conversation_vector_segments,
            conversation_vector_memberships, conversation_embeddings,
            public.andy_vector_attachment_provenance TO {role}"""))

    class Reader(Session):
        pass

    @event.listens_for(Reader, "after_begin")
    def set_role(session, transaction, connection):
        connection.exec_driver_sql(f"SET LOCAL ROLE {role}")

    try:
        yield sessionmaker(bind=factory.kw["bind"], class_=Reader)
    finally:
        with factory.begin() as admin:
            admin.execute(text(f"DROP OWNED BY {role}"))
            admin.execute(text(f"DROP ROLE {role}"))
            if install:
                admin.execute(text("DROP VIEW public.andy_vector_attachment_provenance"))


VOICE_QUERY = "A reunião para revisar o orçamento foi marcada para a próxima semana."


@pytest.fixture
def receipt_voices(db, provider):
    from attention_router.application.platform.artifacts import ArtifactReceiptInput, register_artifact_receipt
    from attention_router.infrastructure.artifact_models import ArtifactReceiptRow, ArtifactRow
    from attention_router.infrastructure.models import ResourceRow

    factory, tenant, conversation, message = db
    one, two = tenant(), tenant()
    fixtures = []
    artifact_ids, resource_ids = set(), set()
    try:
        for tenant_id, account in ((one, "fixture-a"), (one, "fixture-b"), (two, "fixture-a")):
            conversation_id = conversation(tenant_id, account=account)
            message_id, inbound_id = message(tenant_id, conversation_id, VOICE_QUERY, account=account, voice=True)
            with factory.begin() as session:
                result = register_artifact_receipt(session, ArtifactReceiptInput(
                    tenant_id=tenant_id, content_sha256="a" * 64, artifact_kind="AUDIO",
                    mime_type="audio/ogg", size_bytes=16, storage_provider="fixture",
                    storage_reference="audio-fixture", source_channel="channel.whatsapp",
                    source_account=account, external_receipt_id=f"whatsapp:{message_id}:media",
                    received_at=NOW,
                ))
                source = session.get(ConversationMessageRow, message_id)
                source.metadata_json = {
                    **source.metadata_json, "artifact_receipt_id": result.receipt.id,
                    "artifact_id": result.artifact.id, "attachment_state": "READY",
                }
                inbound = session.get(InboundEventRow, inbound_id)
                inbound.external_event_id = message_id
                inbound.payload = {"metadata": {"source_account": account}}
                stt = session.scalar(text("SELECT id FROM voice_transcriptions WHERE inbound_event_id=:id"),
                                     {"id": inbound_id})
                fixtures.append((tenant_id, account, message_id, result.receipt.id, stt, inbound_id))
                artifact_ids.add(result.artifact.id)
                resource_ids.add(result.artifact.resource_id)
        drain(factory, provider)
        yield fixtures
    finally:
        with factory.begin() as session:
            for model, ids in (
                (ArtifactReceiptRow, [row[3] for row in fixtures]), (ArtifactRow, artifact_ids), (ResourceRow, resource_ids),
            ):
                for row_id in ids:
                    session.delete(session.get(model, row_id))
                session.flush()


def voice_matches(reader, provider, scope, mode):
    from attention_router.application import conversation_hybrid

    tenant, account, *_ = scope
    with reader() as session:
        params = dict(tenant_id=tenant, source_account=account, query=VOICE_QUERY, provider=provider)
        result = (v.search(session, **params) if mode == "vector"
                  else conversation_hybrid.search(session, search_mode=mode, **params))
        return {(m["id"], m["transcription_id"]) for r in result["results"] for m in r["messages"]}


def test_voice_receipt_validity_and_scope_with_read_only_role(db, provider, receipt_voices, vector_reader):
    from attention_router.infrastructure.artifact_models import ArtifactReceiptRow

    first, second, foreign = receipt_voices
    modes = ("vector", "lexical", "hybrid")
    with vector_reader() as reader:
        for table in ("artifacts", "artifact_receipts", "inbound_events", "queue"):
            assert not reader.scalar(text("SELECT has_any_column_privilege(current_user,:table,'SELECT')"),
                                     {"table": table})
        assert not reader.scalar(text("SELECT has_table_privilege('conversation_messages','UPDATE')"))
        assert not reader.scalar(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"))
        for tenant, account, message, *_ in receipt_voices:
            v.scope(reader, tenant, account)
            assert set(reader.scalars(text("SELECT message_id FROM public.andy_vector_attachment_provenance"))) == {message}

    for row in receipt_voices:
        for mode in modes:
            assert voice_matches(vector_reader, provider, row, mode) == {(row[2], row[4])}

    with db[0].begin() as admin:
        expired = admin.get(ArtifactReceiptRow, first[3])
        expired.metadata_json = {**expired.metadata_json, "context_expires_at": (datetime.now(timezone.utc)
                                  - timedelta(minutes=1)).isoformat()}
        withdrawn = admin.get(ArtifactReceiptRow, second[3])
        withdrawn.metadata_json = {**withdrawn.metadata_json, "context_evidence_withdrawn": True}
    for mode in modes:
        assert not voice_matches(vector_reader, provider, first, mode)
        assert not voice_matches(vector_reader, provider, second, mode)
        assert voice_matches(vector_reader, provider, foreign, mode) == {(foreign[2], foreign[4])}


@pytest.mark.parametrize("mode", ["vector", "hybrid"])
def test_receipt_source_revision_at_materialization(db, provider, receipt_voices, vector_reader, monkeypatch, mode):
    first, *_ = receipt_voices
    check = v.revalidated_results
    changed = []

    def update_source_then_check(session, tenant, account, results, versions):
        assert results and not changed
        with db[0].begin() as admin:
            source = admin.get(ConversationMessageRow, first[2])
            previous = source.context_revision
            # The text and segment hashes stay identical. The source revision changes.
            source.metadata_json = {**source.metadata_json, "capture_contract": "synthetic-revision-two"}
            admin.flush()
            admin.refresh(source)
            assert source.context_revision != previous
        changed.append(True)
        return check(session, tenant, account, results, versions)

    monkeypatch.setattr(v, "revalidated_results", update_source_then_check)
    assert not voice_matches(vector_reader, provider, first, mode)
    assert changed


def test_voice_rejects_stt_from_another_account(db, provider, receipt_voices, vector_reader):
    first, second, _ = receipt_voices
    with db[0].begin() as admin:
        msg = admin.get(ConversationMessageRow, first[2])
        msg.metadata_json = {**msg.metadata_json, "source_inbound_event_id": second[5]}
    drain(db[0], provider)
    assert not voice_matches(vector_reader, provider, first, "vector")
    assert voice_matches(vector_reader, provider, second, "vector") == {(second[2], second[4])}
