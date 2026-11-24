"""PostgreSQL proof for the alternate consented candidate path."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
from threading import Event
from time import monotonic, sleep

import pytest
from andy_client_context import enroll_fixture_fence
from sqlalchemy import event, func, select, text

from attention_router.application import conversation_vectors as vectors
from attention_router.application.client_context import ClientContextReader, ClientContextScope
from attention_router.application.context_compiler import compile_evidence_candidates
from attention_router.application.context_consent import RETENTION_LAYERS, LabConsent, SourceConsent
from attention_router.application.context_validity import ContextValidity
from attention_router.application.conversation_evidence import retrieve_evidence_candidates
from attention_router.application.conversation_evidence_staged import StagedConversationEvidenceReader
from attention_router.application.staged_read import StagedReadRejected
from attention_router.infrastructure.artifact_models import ArtifactReceiptRow
from attention_router.infrastructure.client_bootstrap_models import ClientTenantMembershipRow
from attention_router.infrastructure.models import (
    AgentExecutionIntentRow, CapabilityGrantRow, ConversationMessageRow, ExecutionIntentRow, FactRow,
    OutboxMessageRow, VoiceTranscriptionRow,
)
from test_cognitive_observation import START, _attachment
from test_passive_vectors import provider  # noqa: F401
from test_postgres_client_context import context_reader, enroll_device, issue  # noqa: F401
from test_postgres_client_session import NOW, service
from test_postgres_cognitive_lab import enroll_role, lab  # noqa: F401


pytestmark = pytest.mark.postgres


@pytest.fixture
def evidence_world(Session, lab, context_reader, tmp_path, request):  # noqa: F811
    enroll_fixture_fence(Session, request)
    _, _, _, _, _, pilot, scope, _, _, configuration = lab
    role, reader = context_reader
    tenant, account, owner = (scope[key] for key in ("tenant_id", "source_account", "owner_human_identity_id"))
    deadline = datetime.now(UTC) + timedelta(days=30)
    read_time = START + timedelta(days=8)
    grants, evidence_ids, receipts = [], {}, {}
    engine = reader.kw["bind"]
    with Session.begin() as session:
        session.execute(text(f"GRANT SELECT ON conversation_vector_segments, conversation_vector_memberships, "
                             f"conversation_embeddings TO {role}"))
        session.add(ClientTenantMembershipRow(
            id="fixture-other-owner", human_identity_id=owner, tenant_id="other",
            role="OWNER", status="ACTIVE", created_at=START, updated_at=START,
        ))
    owner_device = enroll_device(Session)
    issued = issue(Session, owner_device, NOW, tenant_id=tenant)
    scope_ref = "ctx_" + "0" * 32

    def seed(label, content, *, conversation=None, sender="fixture-peer", kind="text",
             permitted=True, expired_receipt=False, expired_layer=None, source_account=account, source_tenant=tenant):
        with Session.begin() as session:
            mid, inbound, registered = _attachment(
                session, label, 1, content=content, sender=sender,
                conversation=f"wwebjs:{conversation or label}", account=source_account, tenant_id=source_tenant,
                content_sha256=vectors.digest(label), media_kind="ptt" if kind == "voice" else "image",
            )
            message = session.get(ConversationMessageRow, mid)
            message.message_type = "VOICE" if kind == "voice" else "TEXT"
            message.text = content
            if kind == "voice":
                session.add(VoiceTranscriptionRow(
                    id=f"fixture-transcript:{label}", tenant_id=source_tenant, inbound_event_id=inbound.id,
                    status="READY", transcript_text=content, attempt_count=1, created_at=START, updated_at=START,
                ))
            if expired_receipt:
                registered.receipt.metadata_json = {"context_expires_at": read_time.isoformat()}
            evidence_ids[mid] = label
            receipts[label] = registered.receipt.id
            if permitted:
                grants.append(SourceConsent(
                    decision_ref=f"synthetic:consent:{label}", tenant_id=source_tenant,
                    owner_id=owner, source_account=source_account, conversation_id=message.conversation_id,
                    message_id=mid, receipt_id=registered.receipt.id, purpose="prospective_context",
                    starts_at=message.sent_at, expires_at=deadline, sensitivity="NORMAL",
                    retention=tuple((layer, read_time if layer == expired_layer else deadline)
                                    for layer in sorted(RETENTION_LAYERS)),
                ))
        return mid

    def index(embedder):
        for _ in range(180):
            with Session() as session:
                pending = session.scalar(text(
                    "SELECT count(*) FROM queue WHERE kind=:kind AND status IN ('PENDING','PROCESSING')"
                ), {"kind": vectors.KIND})
            if not pending:
                return
            vectors.cycle(Session, embedder)
        raise AssertionError("Synthetic indexing did not drain")

    def wire(read, *, staged_options=None, asgi=False):
        consent = LabConsent(tmp_path / "vector-consents.sqlite", tuple(grants))
        consent.initialize()
        boundary = ClientContextReader(
            reader, role=role, client_sessions=service(),
            scope_registry={scope_ref: ClientContextScope(
                tenant, owner, scope["binding_id"], account, scope["start"], configuration()["resource_id"],
            )}, consent=consent, epoch=pilot._epoch, read=read,
            clock=lambda: read_time, auth_clock=lambda: NOW,
        )

        def query(question):
            return boundary.query(bearer=issued.session_token, scope_ref=scope_ref, question=question)

        if staged_options is not None:
            staged_reader = StagedConversationEvidenceReader(boundary, **staged_options)

            def staged_query(question):
                bodies = []
                receipt = staged_reader.query_and_deliver(
                    bearer=issued.session_token, scope_ref=scope_ref, question=question, deliver=bodies.append,
                )
                assert len(bodies) == 1 and receipt.body_size == len(bodies[0])
                return json.loads(bodies[0])

            if asgi:
                from fastapi import FastAPI
                from fastapi.testclient import TestClient
                from starlette.routing import Route
                from attention_router.api.v1.client_context_staged import StagedContextEndpoint

                application = FastAPI(routes=[Route(
                    "/api/v1/client/context/staged-evidence",
                    StagedContextEndpoint(staged_reader), methods=["POST"],
                )])

                def http_query(question):
                    with TestClient(application) as client:
                        return client.post(
                            "/api/v1/client/context/staged-evidence",
                            json={"scope_ref": scope_ref, "question": question},
                            headers={"Authorization": "Bearer " + issued.session_token},
                        )

                return query, staged_query, http_query, consent
            return query, staged_query, consent
        return query, consent

    return seed, index, wire, evidence_ids, receipts, scope, pilot, engine, read_time


def _retrieve(context, epoch, question, embedder):
    candidates = retrieve_evidence_candidates(context, epoch, question, provider=embedder)
    packet = compile_evidence_candidates(
        context.arguments["tenant_id"], question, candidates, now=context.arguments["now"], token_budget=5500,
    )
    return candidates, packet


def test_candidate_eligibility_and_concurrent_withdrawal(Session, evidence_world, provider):  # noqa: F811
    seed, index, wire, labels, receipts, scope, pilot, _, read_time = evidence_world
    good = seed("eligible", "Bia confirmou a reunião às 15 horas.")
    seed("redacted", "Senha da reunião de sábado é um segredo sintético.")
    seed("mixed-a", "Bia confirmou a reunião às 16 horas.", conversation="mixed")
    seed("mixed-b", "Trecho vedado da reunião com horário complementar.", conversation="mixed", permitted=False)
    participant_a = seed("participant-a", "Bia confirmou a reunião às 17 horas.", conversation="participants")
    participant_b = seed("participant-b", "Davi não confirmou a reunião às 17 horas.",
                         conversation="participants", sender="another-participant")
    seed("expired", "Bia confirmou a reunião às 18 horas.", expired_receipt=True)
    seed("expired-embedding", "Bia confirmou a reunião às 19 horas.", expired_layer="embedding")
    seed("expired-stt", "Bia confirmou a reunião às 20 horas.", kind="voice", expired_layer="transcription")
    seed("foreign-tenant", "Bia confirmou a reunião às 21 horas.", source_tenant="other")
    seed("foreign-account", "Bia confirmou a reunião às 22 horas.", source_account="other-account")
    changed = seed("changed", "Bia confirmou a reunião às 23 horas.")
    with Session.begin() as session:
        session.get(ConversationMessageRow, participant_b).reply_to_message_id = participant_a
    index(provider)

    with Session.begin() as session:
        session.execute(text("""
            UPDATE conversation_vector_segments SET content_text='alteração sintética fora do hash'
            WHERE id IN (SELECT segment_id FROM conversation_vector_memberships WHERE message_id=:id)
        """), {"id": changed})

    def effects():
        with Session() as session:
            return tuple(session.scalar(select(func.count()).select_from(model)) for model in (
                OutboxMessageRow, CapabilityGrantRow, ExecutionIntentRow, AgentExecutionIntentRow, FactRow,
            ))

    initial = effects()
    reading, advance, writer_started = Event(), Event(), Event()
    writer_pids = []
    captured = []

    def read(context, epoch, question, _resource):
        candidates, packet = _retrieve(context, epoch, question, provider)
        filtered = retrieve_evidence_candidates(
            context, epoch, question, provider=provider, participant="fixture-peer",
        )
        assert not {"participant-a", "participant-b"} & {labels[x.source_ref] for c in filtered for x in c.excerpts}
        assert packet.retrieval_method == "AUTHORIZED_EVIDENCE_CANDIDATES_V1"
        assert all(not item.provenance["grants_authority"] and not item.path for item in packet.items)
        for candidate in candidates:
            for excerpt in candidate.excerpts:
                if excerpt.source_ref == participant_b:
                    assert excerpt.reply_to_ref == participant_a
        sources = {
            labels[ex.source_ref] for c in candidates for ex in c.excerpts
        }
        captured.append(sources)
        if not reading.is_set():
            reading.set()
            assert advance.wait(6)
        return sources

    lookup, consent = wire(read)
    with consent.current() as (_, permits):
        write_permits = permits

    def withdraw():
        with Session.begin() as session:
            writer_pids.append(session.scalar(text("SELECT pg_backend_pid()")))
            writer_started.set()
            context = ContextValidity(session, **scope, now=read_time, source_allowed=write_permits)
            pilot._epoch.advance(context, context.revoke_receipt, receipts["eligible"])

    question = "Que horas foi confirmada a reunião?"
    with ThreadPoolExecutor(max_workers=2) as pool:
        before = pool.submit(lookup, question)
        try:
            assert reading.wait(6)
            turn = pool.submit(withdraw)
            assert writer_started.wait(6)
            stop_at = monotonic() + 3
            with Session() as monitor:
                while not monitor.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": writer_pids[0]}):
                    assert monotonic() < stop_at and not turn.done()
                    sleep(0.025)
        finally:
            advance.set()
        assert before.result(timeout=10) == {"eligible", "participant-a", "participant-b"}
        turn.result(timeout=10)
    assert lookup(question) == {"participant-a", "participant-b"}
    assert consent.revoke(participant_a)
    assert lookup(question) == set()
    assert effects() == initial
    assert good in labels and captured
    with Session() as session:
        assert session.get(ArtifactReceiptRow, receipts["eligible"]).metadata_json["context_evidence_withdrawn"]
    print("composition_security owner,tenant,account,participant,mixed,retention,hash,receipt,postgres_lock,no_effects")


def _json_packet(packet):
    return json.loads(json.dumps(packet.internal_payload(), default=datetime.isoformat))


@pytest.mark.parametrize("lab", ["client-query"], indirect=True)
def test_staged_e5_pgvector_parity_and_receipt_change(Session, evidence_world, provider, monkeypatch):  # noqa: F811
    from attention_router.application import conversation_evidence_staged as staged

    seed, index, wire, labels, receipts, _, _, engine, _ = evidence_world
    seed("eligible", "Bia confirmou a reunião às 15 horas.")
    seed("voice", "A reunião ficou marcada para 16 horas.", kind="voice")
    seed("expired", "Bia confirmou a reunião às 18 horas.", expired_receipt=True)
    seed("denied", "Bia confirmou a reunião às 19 horas.", permitted=False)
    seed("wrong-receipt", "A reunião ficou marcada para 20 horas.")
    with Session.begin() as writer:
        writer.get(ArtifactReceiptRow, receipts["wrong-receipt"]).external_receipt_id = "synthetic:unrelated"
    index(provider)

    def read(context, epoch, question, _resource):
        return _json_packet(_retrieve(context, epoch, question, provider)[1])

    control_query, query, http_query, _ = wire(
        read, staged_options=dict(provider=provider, reserve_query=lambda _scope: True, token_budget=5500), asgi=True,
    )
    # Wire the same consent enrollment and scope for both paths.
    active = 0

    def begin(_connection):
        nonlocal active
        active += 1

    def end(_connection):
        nonlocal active
        active -= 1

    event.listen(engine, "begin", begin)
    event.listen(engine, "commit", end)
    event.listen(engine, "rollback", end)
    rank = staged._compile

    def compute(*args, **kwargs):
        assert active == 0
        return rank(*args, **kwargs)

    monkeypatch.setattr(staged, "_compile", compute)
    question = "Em que horário ficou a reunião?"
    result = query(question)
    assert result == query(question) == control_query(question)
    http_result = http_query(question)
    assert http_result.status_code == 200
    assert http_result.headers["cache-control"] == "no-store"
    assert http_result.json() == result
    assert query("Ela confirmou isso?") == control_query("Ela confirmou isso?")
    assert {
        labels[excerpt["source_ref"]]
        for item in result["items"] for excerpt in item["attributes"]["excerpts"]
    } == {"eligible", "voice"}

    targets = iter(("eligible", "voice"))

    def mutate(*args, **kwargs):
        result = compute(*args, **kwargs)
        # Each request revokes a different still-valid source *after capture*.
        with Session.begin() as writer:
            receipt = writer.get(ArtifactReceiptRow, receipts[next(targets)])
            receipt.metadata_json = {**receipt.metadata_json, "context_evidence_withdrawn": True}
        return result

    monkeypatch.setattr(staged, "_compile", mutate)
    with pytest.raises(StagedReadRejected, match="SUPPORT_CHANGED"):
        query(question)
    # When the source changes before disclosure, the HTTP body remains empty.
    rejected = http_query(question)
    assert rejected.status_code == 503 and rejected.content == b""
