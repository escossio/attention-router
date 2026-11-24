"""Disposable PostgreSQL tests using real ClientSession and staged evidence."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from secrets import token_hex
from threading import Event

import pytest
from andy_client_context import enroll_fixture_fence
from andy_client_context import capture_evidence, rank_evidence, read, verify_evidence
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from attention_router.application.client_context import ClientContextReader, ClientContextScope
from attention_router.application.client_context_staged import StagedClientContextReader
from attention_router.application.client_session import ClientSessionAuthorityRejected
from attention_router.application.context_pg_consent import PgSourceConsent, PgSourceConsentWriter
from attention_router.infrastructure.artifact_models import ArtifactReceiptRow
from attention_router.infrastructure.client_bootstrap_models import ClientTenantMembershipRow
from attention_router.infrastructure.context_consent_models import ContextConsentScopeRow
from attention_router.infrastructure.models import ConversationMessageRow
from test_postgres_client_context import context_reader, enroll_device, issue  # noqa: F401
from test_postgres_client_session import NOW, service
from test_postgres_cognitive_lab import lab  # noqa: F401
from test_postgres_context_preactivation import enroll_role  # noqa: F401
from test_passive_vectors import provider  # noqa: F401

pytestmark = pytest.mark.postgres


@pytest.fixture
def consent_clients(Session, lab, context_reader, request):  # noqa: F811
    enroll_fixture_fence(Session, request)
    ids, clock, observe, _, _, pilot, scope, _, no_effects, config = lab
    role, restricted = context_reader
    writer_role = "fixture_consent_" + token_hex(6)
    password = token_hex(24)
    scope_row = ClientContextScope(
        scope["tenant_id"], scope["owner_human_identity_id"],
        scope["binding_id"], scope["source_account"], scope["start"], config()["resource_id"],
    )
    with Session.begin() as admin:
        admin.execute(text(f"CREATE ROLE {writer_role} LOGIN PASSWORD '{password}'"))
        admin.execute(text(f"GRANT {role} TO {writer_role}"))
        admin.execute(text(f"GRANT SELECT ON context_source_consents, context_consent_scopes TO {role}"))
        admin.execute(text(f"GRANT UPDATE(id) ON context_consent_scopes TO {role}"))
        admin.execute(text(
            f"GRANT INSERT, UPDATE ON context_source_consents, context_consent_scopes TO {writer_role}"
        ))
        allowed_scope = (
            f"tenant_id = '{scope_row.tenant_id}' AND owner_id = '{scope_row.owner_id}' "
            f"AND source_account = '{scope_row.source_account}'"
        )
        policies = {
            "context_consent_scopes": allowed_scope,
            "context_source_consents": "EXISTS (SELECT 1 FROM context_consent_scopes WHERE id = scope_id)",
        }
        for table, allowed in policies.items():
            admin.execute(text(f"CREATE POLICY consent_test_read ON {table} FOR SELECT TO {role} USING ({allowed})"))
            admin.execute(text(
                f"CREATE POLICY consent_test_write ON {table} TO {writer_role} "
                f"USING ({allowed}) WITH CHECK ({allowed})"
            ))
        admin.execute(text(
            f"CREATE POLICY consent_test_lock ON context_consent_scopes FOR UPDATE TO {role} "
            f"USING ({allowed_scope}) WITH CHECK (false)"
        ))
    engine = create_engine(Session.kw["bind"].url.set(username=writer_role, password=password), hide_parameters=True)

    def base(sessions, role):
        return ClientContextReader(
            sessions, role=role, client_sessions=service(),
            scope_registry={
                "owner": scope_row,
                "ctx_" + "1" * 32: scope_row,
                "ctx_" + "2" * 32: replace(scope_row, tenant_id="other"),
                "ctx_" + "3" * 32: replace(scope_row, owner_id="unrelated"),
                "ctx_" + "4" * 32: replace(scope_row, resource_id="unrelated"),
                "foreign-tenant": replace(scope_row, tenant_id="other"),
                "foreign-owner": replace(scope_row, owner_id="unrelated"),
                "foreign-account": replace(scope_row, source_account="other"),
            },
            consent=None, epoch=pilot._epoch, read=read, clock=pilot._clock, auth_clock=lambda: NOW,
        )

    def staged(compute=rank_evidence, capture=capture_evidence):
        return StagedClientContextReader(
            base(restricted, role), pg_consent=PgSourceConsent(),
            capture_fn=capture, verify_fn=verify_evidence, compute_fn=compute,
        )

    def decision(name):
        with Session() as db:
            message = db.get(ConversationMessageRow, ids[name])
            receipt = db.get(ArtifactReceiptRow, message.metadata_json["artifact_receipt_id"])
            return dict(
                decision_ref=f"fixture:{name}", message_id=message.id, conversation_id=message.conversation_id,
                receipt_id=receipt.id, message_revision=message.context_revision,
                receipt_revision=receipt.context_revision, starts_at=message.sent_at,
                expires_at=message.sent_at + timedelta(days=4), sensitivity="NORMAL",
                retention=tuple(
                    (layer, message.sent_at + timedelta(days=2)) for layer, _ in config()["grants"][0]["retention"]
                ),
            )

    try:
        bearer = issue(Session, enroll_device(Session), NOW).session_token
        observe("plan")
        clock(day=2)
        observe("proposal")
        clock(day=3)
        observe("confirm")
        args = dict(bearer=bearer, scope_ref="owner")
        yield args, PgSourceConsentWriter(base(sessionmaker(engine, autoflush=False), writer_role)), staged, decision, clock
        no_effects()
    finally:
        engine.dispose()
        with Session.begin() as admin:
            admin.execute(text("DROP POLICY consent_test_lock ON context_consent_scopes"))
            for table in ("context_consent_scopes", "context_source_consents"):
                admin.execute(text(f"DROP POLICY consent_test_read ON {table}"))
                admin.execute(text(f"DROP POLICY consent_test_write ON {table}"))
            admin.execute(text(f"DROP OWNED BY {writer_role}"))
            admin.execute(text(f"DROP ROLE {writer_role}"))


@pytest.mark.parametrize("lab", ["client-query"], indirect=True)
def test_grant_is_explicit_scoped_and_idempotent(Session, consent_clients):
    args, writer, staged, decision, clock = consent_clients
    delivered = []

    def ask(reader=None, **overrides):
        (reader or staged()).query_and_deliver(**(args | overrides), question="domingo", deliver=delivered.append)

    with pytest.raises(ValueError, match="SCOPE_UNAVAILABLE"):
        ask()
    valid = decision("confirm")
    for field, invalid in (
        ("receipt_id", decision("proposal")["receipt_id"]),
        ("message_revision", "wrong"),
        ("receipt_revision", "wrong"),
        ("conversation_id", "wrong"),
    ):
        with pytest.raises(ValueError, match="SOURCE_INELIGIBLE"):
            writer.grant(**args, **(valid | {field: invalid}))
    for name in ("other_account", "other_tenant"):
        with pytest.raises(ValueError, match="SOURCE_UNAVAILABLE"):
            writer.grant(**args, **decision(name))
    for ref in ("foreign-tenant", "foreign-owner"):
        with pytest.raises(ClientSessionAuthorityRejected):
            writer.grant(**(args | {"scope_ref": ref}), **valid)
        with pytest.raises(ClientSessionAuthorityRejected):
            ask(scope_ref=ref)
    assert delivered == []
    assert writer.grant(**args, **valid)
    assert not writer.grant(**args, **valid)
    with Session() as db:
        assert db.scalars(select(ContextConsentScopeRow.revision)).all() == [1]
    with pytest.raises(ValueError, match="SCOPE_UNAVAILABLE"):
        ask(scope_ref="foreign-account")

    def wrong_version(context, epoch, question, resource):
        result = capture_evidence(context, epoch, question, resource)
        return replace(result, sources=(replace(result.sources[0], receipt_revision="wrong"),))

    with pytest.raises(ValueError, match="SOURCE_STALE"):
        ask(staged(capture=wrong_version))
    assert delivered == []
    ask()
    assert len(delivered) == 1 and valid["message_id"].encode() in delivered[0]
    assert decision("proposal")["message_id"].encode() not in delivered[0]
    clock(day=5)
    with pytest.raises(ValueError, match="NO_ELIGIBLE_SOURCE"):
        ask()
    assert len(delivered) == 1

    with Session.begin() as db:
        db.scalar(select(ClientTenantMembershipRow).where(
            ClientTenantMembershipRow.human_identity_id == "fixture-human-owner",
        )).role = "MEMBER"
    with pytest.raises(ClientSessionAuthorityRejected):
        writer.revoke(**args, message_id=valid["message_id"])
    with pytest.raises(ClientSessionAuthorityRejected):
        ask()


@pytest.mark.parametrize("lab", ["client-query"], indirect=True)
@pytest.mark.parametrize("initial_grant", [True, False])
def test_revocation_wins_before_delivery_or_first_grant(Session, consent_clients, initial_grant):
    args, writer, staged, decision, _ = consent_clients
    valid = decision("confirm")
    revoke = args | {"message_id": valid["message_id"]}
    if not initial_grant:
        assert writer.revoke(**revoke)
        assert not writer.revoke(**revoke)
        with pytest.raises(ValueError, match="CONSENT_REVOKED"):
            writer.grant(**args, **valid)
        with pytest.raises(ValueError, match="NO_ELIGIBLE_SOURCE"):
            staged().query_and_deliver(**args, question="domingo", deliver=lambda _: pytest.fail("disclosed"))
        return

    assert writer.grant(**args, **valid)
    started = Event()
    delivered = []

    def withdraw():
        started.set()
        return writer.revoke(**revoke)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = []

        def capture(context, epoch, question, resource):
            with pytest.raises(DBAPIError) as blocked, Session.begin() as db:
                db.execute(text("SELECT id FROM context_consent_scopes FOR UPDATE NOWAIT"))
            assert blocked.value.orig.sqlstate == "55P03"
            pending.append(pool.submit(withdraw))
            assert started.wait(1) and not pending[0].done()
            return capture_evidence(context, epoch, question, resource)

        def compute(evidence, question):
            assert pending[0].result(timeout=5)
            return rank_evidence(evidence, question)

        with pytest.raises(ValueError, match="REVISION_STALE"):
            staged(compute=compute, capture=capture).query_and_deliver(
                **args, question="domingo", deliver=delivered.append,
            )

    assert delivered == []
    with Session() as db:
        assert db.scalars(select(ContextConsentScopeRow.revision)).all() == [2]
    assert not writer.revoke(**revoke)
    with pytest.raises(ValueError, match="CONSENT_REVOKED"):
        writer.grant(**args, **valid)


@pytest.mark.parametrize("lab", ["client-query"], indirect=True)
@pytest.mark.parametrize("deadline", ["grant", "retention"])
def test_clock_expiration_during_compute_blocks_disclosure(consent_clients, deadline):
    args, writer, staged, decision, clock = consent_clients
    valid = decision("confirm")
    if deadline == "grant":
        valid["expires_at"] = valid["starts_at"] + timedelta(days=1)
    assert writer.grant(**args, **valid)

    def compute(evidence, question):
        clock(day=4 if deadline == "grant" else 5)
        return rank_evidence(evidence, question)

    with pytest.raises(ValueError, match="STAGED_SUPPORT_CHANGED"):
        staged(compute=compute).query_and_deliver(
            **args, question="domingo", deliver=lambda _: pytest.fail("disclosed"),
        )


@pytest.mark.parametrize("lab", ["client-query"], indirect=True)
def test_delivery_holds_consent_lock_until_callback_returns(Session, consent_clients):
    args, writer, staged, decision, _ = consent_clients
    valid = decision("confirm")
    assert writer.grant(**args, **valid)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = []
        started = Event()

        def withdraw():
            started.set()
            return writer.revoke(**args, message_id=valid["message_id"])

        def deliver(body):
            assert valid["message_id"].encode() in body
            with pytest.raises(DBAPIError) as blocked, Session.begin() as db:
                db.execute(text("SELECT id FROM context_consent_scopes FOR UPDATE NOWAIT"))
            assert blocked.value.orig.sqlstate == "55P03"
            pending.append(pool.submit(withdraw))
            assert started.wait(1) and not pending[0].done()

        receipt = staged().query_and_deliver(**args, question="domingo", deliver=deliver)
        assert receipt.body_size > 0 and pending[0].result(timeout=5)
    with pytest.raises(ValueError, match="NO_ELIGIBLE_SOURCE"):
        staged().query_and_deliver(**args, question="domingo", deliver=lambda _: pytest.fail("disclosed"))


@pytest.mark.parametrize("lab", ["client-query"], indirect=True)
def test_modified_message_cannot_reuse_grant(Session, consent_clients):
    args, writer, staged, decision, _ = consent_clients
    valid = decision("confirm")
    assert writer.grant(**args, **valid)

    def compute(evidence, question):
        with Session.begin() as db:
            message = db.get(ConversationMessageRow, valid["message_id"])
            message.text = "Replacement synthetic text"
        return rank_evidence(evidence, question)

    with pytest.raises(ValueError, match="STAGED_SUPPORT_CHANGED"):
        staged(compute=compute).query_and_deliver(
            **args, question="domingo", deliver=lambda _: pytest.fail("disclosed"),
        )


@pytest.mark.parametrize("lab", ["client-query"], indirect=True)
@pytest.mark.parametrize("transport", ["testclient", "loopback"])
def test_pg_consent_e5_asgi_full_chain(Session, consent_clients, provider, monkeypatch, transport):  # noqa: F811
    """One integrated chain: explicit grant -> real E5 -> ASGI -> concurrent revocation."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.routing import Route

    from attention_router.api.v1.client_context_staged import StagedContextEndpoint
    from attention_router.application import conversation_evidence_staged as staged_e5
    from attention_router.application import conversation_vectors as vectors
    from attention_router.application.conversation_evidence_staged import StagedConversationEvidenceReader

    args, writer, staged, decision, _ = consent_clients
    # The older cognitive fixture archives MEDIA captions. E5 intentionally
    # indexes only TEXT or VOICE; convert these two synthetic captions to
    # indexable text before taking and sealing the explicit consent decision.
    with Session.begin() as db:
        for name in ("proposal", "confirm"):
            source = decision(name)
            message = db.get(ConversationMessageRow, source["message_id"])
            assert message is not None and message.text
            message.message_type = "TEXT"

    grant_a = decision("proposal")
    grant_b = decision("confirm")
    assert writer.grant(**args, **grant_a)
    assert writer.grant(**args, **grant_b)

    # Index only the synthetic, explicitly granted corpus in a disposable DB.
    for _ in range(40):
        with Session() as session:
            pending = session.scalar(text(
                "SELECT count(*) FROM queue WHERE kind=:kind AND status IN ('PENDING','PROCESSING')"
            ), {"kind": vectors.KIND})
        if not pending:
            break
        vectors.cycle(Session, provider)
    else:
        pytest.fail("INDEX_NOT_DRAINED")

    baseline = staged()._pipeline._authority._reader
    from test_postgres_client_session import service as session_service

    valid_scope = "ctx_" + "1" * 32
    original = ClientContextReader(
        baseline._sessions, role=baseline._role, client_sessions=session_service(),
        scope_registry={valid_scope: baseline._scopes["owner"]},
        consent=None, epoch=baseline._epoch, read=baseline._read,
        clock=baseline._clock, auth_clock=baseline._auth_clock,
    )
    # The context reader fixture already grants the narrow row-lock
    # privileges. This path only needs to read the vector index as well.
    with Session.begin() as admin:
        admin.execute(text(
            "GRANT SELECT ON conversation_vector_segments, conversation_vector_memberships, "
            f"conversation_embeddings TO {baseline._role}"
        ))

    reader = StagedConversationEvidenceReader(
        original,
        pg_consent=PgSourceConsent(),
        provider=provider,
        reserve_query=lambda scope: True,
        token_budget=4000,
    )
    app = FastAPI(routes=[Route(
        "/api/v1/client/context/staged-evidence",
        StagedContextEndpoint(reader), methods=["POST"],
    )])

    def query():
        uri = "/api/v1/client/context/staged-evidence"
        payload = {"question": "Em que data ficou a viagem?", "scope_ref": valid_scope}
        headers = {"Authorization": "Bearer " + args["bearer"]}
        if transport == "testclient":
            with TestClient(app) as client:
                return client.post(uri, json=payload, headers=headers)

        # Real ASGI over a loopback-only TCP socket; still a disposable CI DB.
        from socket import AF_INET, SOCK_STREAM, socket
        from threading import Thread
        from time import monotonic, sleep
        import httpx
        import uvicorn

        with socket(AF_INET, SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, access_log=False,
            log_level="critical", lifespan="off", timeout_keep_alive=1,
        ))
        thread = Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 5
            while not server.started and monotonic() < deadline:
                sleep(0.02)
            assert server.started
            with httpx.Client(trust_env=False, timeout=10) as client:
                return client.post(
                    f"http://127.0.0.1:{port}{uri}", json=payload, headers=headers,
                )
        finally:
            server.should_exit = True
            thread.join(timeout=6)
            assert not thread.is_alive()

    first = query()
    if first.status_code != 200:
        # Surface the swallowed ASGI error for diagnosis in disposable CI.
        reader.query_and_deliver(
            bearer=args["bearer"], scope_ref=valid_scope,
            question="Em que data ficou a viagem?", deliver=lambda _: None,
        )
    assert first.status_code == 200, (first.status_code, first.content)
    assert first.headers["cache-control"] == "no-store"
    first_body = first.json()
    assert first_body["retrieval_method"] == "AUTHORIZED_EVIDENCE_CANDIDATES_V1"
    assert first_body["items"]

    compile_original = staged_e5._compile
    phantom = "synthetic-index-phantom"
    statements = (
        """INSERT INTO conversation_vector_segments
           (id, tenant_id, source_account, conversation_id, policy_version, content_text,
            content_hash, source_hash, state, generation, start_at, end_at)
           SELECT :phantom, tenant_id, source_account, conversation_id, policy_version, content_text,
                  content_hash, source_hash, state, generation, start_at, end_at
           FROM conversation_vector_segments WHERE id = :original""",
        """INSERT INTO conversation_vector_memberships
           SELECT :phantom, tenant_id, source_account, conversation_id, message_id,
                  ordinal, char_start, char_end, role
           FROM conversation_vector_memberships WHERE segment_id = :original""",
        """INSERT INTO conversation_embeddings
           (segment_id, tenant_id, source_account, model, model_revision, dimensions, content_hash, embedding)
           SELECT :phantom, tenant_id, source_account, model, model_revision, dimensions, content_hash, embedding
           FROM conversation_embeddings WHERE segment_id = :original""",
    )
    with Session() as db:
        original_segment = db.scalars(text(
            "SELECT DISTINCT m.segment_id FROM conversation_vector_memberships m "
            "JOIN conversation_vector_segments s ON s.id = m.segment_id "
            "WHERE m.message_id = :mid AND s.state = 'READY' ORDER BY m.segment_id"
        ), {"mid": grant_b["message_id"]}).first()
        assert original_segment is not None

    def publish_phantom(*args_, **kwargs_):
        result = compile_original(*args_, **kwargs_)
        with Session.begin() as db:
            for statement in statements:
                assert db.execute(text(statement), {"phantom": phantom, "original": original_segment}).rowcount > 0
        return result

    monkeypatch.setattr(staged_e5, "_compile", publish_phantom)
    with pytest.raises(ValueError, match="STAGED_CONVERSATION_SUPPORT_CHANGED"):
        reader.query_and_deliver(
            bearer=args["bearer"], scope_ref=valid_scope, question="Em que data ficou a viagem?",
            deliver=lambda _: pytest.fail("disclosed after a phantom insertion"),
        )
    with Session.begin() as db:
        assert db.execute(text("DELETE FROM conversation_vector_segments WHERE id = :id"), {"id": phantom}).rowcount == 1

    def change_authority_during_compute(*args_, **kwargs_):
        result = compile_original(*args_, **kwargs_)
        assert writer.revoke(**args, message_id=grant_b["message_id"])
        return result

    monkeypatch.setattr(staged_e5, "_compile", change_authority_during_compute)
    second = query()
    assert second.status_code == 503 and second.content == b""
    assert not writer.revoke(**args, message_id=grant_b["message_id"])
