"""Persisted OWNER scope resolved through an actual authenticated ClientSession."""

import pytest
from sqlalchemy import select

from attention_router.application.client_session import (
    ClientSessionAuthorityRejected, ClientSessionUnauthenticated,
)
from attention_router.application.context_owner_scope import AuthenticatedOwnerScopeRegistry
from attention_router.application.client_context_staged import StagedClientContextAuthority
from attention_router.application.context_pg_consent import PgSourceConsent
from attention_router.infrastructure.client_bootstrap_models import ClientTenantMembershipRow
from attention_router.infrastructure.context_consent_models import ContextConsentScopeRow
from test_postgres_client_session import service
from test_postgres_client_context import context_reader  # noqa: F401
from test_postgres_context_preactivation import enroll_role  # noqa: F401
from test_postgres_cognitive_lab import lab  # noqa: F401
from test_postgres_source_consent import consent_clients  # noqa: F401
from test_passive_vectors import provider  # noqa: F401

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("lab", ["client-query"], indirect=True)
def test_registered_scope_requires_current_owner_and_session(Session, consent_clients):  # noqa: F811
    args, writer, _, decision, _ = consent_clients
    source = decision("confirm")
    assert writer.grant(**args, **source)
    with Session() as db:
        persisted = db.scalars(select(ContextConsentScopeRow)).one()
        scope_ref = "ctx_" + persisted.id

    registry = AuthenticatedOwnerScopeRegistry(
        client_sessions=service(),
        clock=writer._reader._clock,
        auth_clock=writer._reader._auth_clock,
    )
    with Session.begin() as db:
        scope = registry.resolve(db, bearer=args["bearer"], scope_ref=scope_ref)
        assert scope.tenant_id == persisted.tenant_id
        assert scope.owner_id == persisted.owner_id
        assert scope.owner_binding_id == persisted.binding_id
        assert scope.source_account == persisted.source_account
        assert scope.resource_id == persisted.resource_id
        assert scope.start == persisted.scope_start

    # Exercise the exact admission seam without a manually populated
    # scope registry. Capture and disclosure have separate proofs.
    authority = StagedClientContextAuthority(
        writer._reader,
        capture_fn=lambda *_: pytest.fail("capture-not-used-in-scope-test"),
        verify_fn=lambda *_: pytest.fail("delivery-not-used-in-scope-test"),
        pg_consent=PgSourceConsent(), owner_scope_registry=registry,
    )
    assert authority.authorize_preparation(
        bearer=args["bearer"], scope_ref=scope_ref,
    ) == scope

    with pytest.raises(ClientSessionAuthorityRejected), Session.begin() as db:
        registry.resolve(db, bearer=args["bearer"], scope_ref="ctx_" + "0" * 64)
    with pytest.raises(ClientSessionAuthorityRejected), Session.begin() as db:
        registry.resolve(db, bearer=args["bearer"], scope_ref="owner-from-client")
    with pytest.raises((ClientSessionUnauthenticated, ClientSessionAuthorityRejected)), Session.begin() as db:
        registry.resolve(db, bearer="cst_" + "x" * 43, scope_ref=scope_ref)

    with Session.begin() as db:
        membership = db.scalars(
            select(ClientTenantMembershipRow).where(
                ClientTenantMembershipRow.human_identity_id == persisted.owner_id,
                ClientTenantMembershipRow.tenant_id == persisted.tenant_id,
            )
        ).one()
        membership.role = "MEMBER"

    with pytest.raises(ClientSessionAuthorityRejected), Session.begin() as db:
        registry.resolve(db, bearer=args["bearer"], scope_ref=scope_ref)


@pytest.mark.parametrize("lab", ["client-query"], indirect=True)
def test_explicit_scope_enrollment_creates_no_source_consent(Session, consent_clients):  # noqa: F811
    from attention_router.infrastructure.context_consent_models import ContextSourceConsentRow
    args, writer, _, decision, _ = consent_clients
    registry = AuthenticatedOwnerScopeRegistry(
        client_sessions=service(),
        clock=writer._reader._clock,
        auth_clock=writer._reader._auth_clock,
    )
    source = decision("confirm")
    start = writer._reader._scopes["owner"].start

    def enroll(message_id, *, bearer=args["bearer"], capture_start=start):
        with writer._reader._sessions.begin() as db:
            return registry.enroll(
                db, bearer=bearer, message_id=message_id, capture_start=capture_start,
            )

    registered = enroll(source["message_id"])
    assert registered.startswith("ctx_")
    assert registered == enroll(source["message_id"])
    with Session.begin() as db:
        row = db.scalars(select(ContextConsentScopeRow)).one()
        assert registered == "ctx_" + row.id
        assert row.revision == 0
        assert db.scalars(select(ContextSourceConsentRow)).all() == []

    with pytest.raises((ClientSessionUnauthenticated, ClientSessionAuthorityRejected)):
        enroll(source["message_id"], bearer="cst_" + "x" * 43)
    with pytest.raises(ClientSessionAuthorityRejected):
        enroll(decision("other_tenant")["message_id"])
    with pytest.raises(ClientSessionAuthorityRejected):
        enroll(source["message_id"], capture_start=start.replace(year=start.year + 5))

    with writer._reader._sessions.begin() as db:
        bound = registry.resolve(db, bearer=args["bearer"], scope_ref=registered)
        assert bound.owner_id == row.owner_id
        assert bound.resource_id == row.resource_id


@pytest.mark.parametrize("lab", ["client-query"], indirect=True)
def test_owner_scope_enrollment_http_is_explicit_and_idempotent(
    Session, consent_clients, monkeypatch,  # noqa: F811
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from attention_router.api.v1.context_source_consent import mount_context_source_consent_for_test
    from attention_router.application.context_pg_consent import PgSourceConsentWriter
    from attention_router.config import settings
    from attention_router.infrastructure.context_consent_models import ContextSourceConsentRow

    args, writer, _, decision, _ = consent_clients
    source = decision("confirm")
    registry = AuthenticatedOwnerScopeRegistry(
        client_sessions=service(),
        clock=writer._reader._clock,
        auth_clock=writer._reader._auth_clock,
    )
    monkeypatch.setattr(settings, "client_session_enabled", True)
    monkeypatch.setattr(settings, "context_source_consent_api_enabled", True)
    app = FastAPI()
    owner_writer = PgSourceConsentWriter(writer._reader, owner_scope_registry=registry)
    assert mount_context_source_consent_for_test(
        app, writer=owner_writer, owner_scope_registry=registry,
        capture_start=writer._reader._scopes["owner"].start,
    )
    path = "/api/v1/context/source-consent/scope/enroll"
    with TestClient(app) as client:
        assert client.post(path, json={"message_id": source["message_id"]}).status_code == 401
        auth = {"Authorization": "Bearer " + args["bearer"]}
        wrong = client.post(path, headers=auth, json={
            "message_id": source["message_id"], "owner_id": "forged",
        })
        assert wrong.status_code == 422 and wrong.content == b""
        foreign = client.post(path, headers=auth, json={
            "message_id": decision("other_tenant")["message_id"],
        })
        assert foreign.status_code == 403
        valid = client.post(path, headers=auth, json={"message_id": source["message_id"]})
        assert valid.status_code == 200 and valid.headers["cache-control"] == "no-store"
        assert valid.json()["scope_ref"].startswith("ctx_")
        again = client.post(path, headers=auth, json={"message_id": source["message_id"]})
        assert again.status_code == 200 and again.json() == valid.json()
        with Session() as db:
            assert db.scalars(select(ContextConsentScopeRow)).one().revision == 0
            assert db.scalars(select(ContextSourceConsentRow)).all() == []

        # Separate, explicit OWNER decision: enrollment itself never grants.
        grant = dict(
            scope_ref=valid.json()["scope_ref"], message_id=source["message_id"],
            decision_ref="fixture:owner-approved",
            expires_at=source["expires_at"].isoformat(),
            retention={layer: date.isoformat() for layer, date in source["retention"]},
        )
        approved = client.post(
            "/api/v1/context/source-consent/grant", headers=auth, json=grant,
        )
        assert approved.status_code == 204 and approved.content == b""
        with Session() as db:
            assert db.scalars(select(ContextConsentScopeRow)).one().revision == 1
            assert len(db.scalars(select(ContextSourceConsentRow)).all()) == 1
        withdrawn = client.post(
            "/api/v1/context/source-consent/revoke", headers=auth,
            json={"scope_ref": grant["scope_ref"], "message_id": grant["message_id"]},
        )
        assert withdrawn.status_code == 204
        with Session() as db:
            assert db.scalars(select(ContextConsentScopeRow)).one().revision == 2
            assert db.scalars(select(ContextSourceConsentRow)).one().revoked_at is not None

        monkeypatch.setattr(settings, "app_env", "production")
        assert client.post(path, headers=auth, json={"message_id": source["message_id"]}).status_code == 404


@pytest.mark.parametrize("lab", ["client-query"], indirect=True)
def test_owner_scope_provisions_independent_epoch_only_after_operator_approval(
    Session, consent_clients, lab, tmp_path,  # noqa: F811
):
    from attention_router.application.context_epoch import ContextEpoch
    from attention_router.application.context_owner_epoch import OwnerEpochProvisioner

    args, writer, _, decision, _ = consent_clients
    registry = AuthenticatedOwnerScopeRegistry(
        client_sessions=service(),
        clock=writer._reader._clock,
        auth_clock=writer._reader._auth_clock,
    )
    source = decision("confirm")
    with writer._reader._sessions.begin() as db:
        scope_ref = registry.enroll(
            db, bearer=args["bearer"], message_id=source["message_id"],
            capture_start=writer._reader._scopes["owner"].start,
        )
    path = tmp_path / "owner-anchor.sqlite"
    key = b"synthetic-operator-anchor-key-for-ci-only-32"
    environment = lab[5]._epoch.environment
    binder = OwnerEpochProvisioner(
        reader=writer._reader, owner_scope_registry=registry,
        environment=environment, authority_id="operator-anchor",
        key=key, path=path,
    )

    proof = binder.preview(bearer=args["bearer"], scope_ref=scope_ref)
    assert proof.scope_ref == scope_ref and len(proof.domain) == 64
    assert len(proof.checkpoint) == 64
    assert not path.exists()

    with pytest.raises(ValueError, match="OPERATOR_APPROVAL_REQUIRED"):
        binder.provision_explicit(
            bearer=args["bearer"], scope_ref=scope_ref, operator_approved=False,
        )
    assert not path.exists()

    anchor = binder.provision_explicit(
        bearer=args["bearer"], scope_ref=scope_ref, operator_approved=True,
    )
    saved, _ = anchor.read(proof.domain, "synthetic-challenge")
    assert saved.checkpoint == proof.checkpoint and saved.revision == 0
    with pytest.raises(ValueError, match="ALREADY_PROVISIONED"):
        binder.provision_explicit(
            bearer=args["bearer"], scope_ref=scope_ref, operator_approved=True,
        )

    epoch = ContextEpoch(
        anchor, authority_id="operator-anchor", environment=environment, key=key,
    )
    with writer._reader._sessions.begin() as db:
        from attention_router.application.client_context_staged import (
            _authorized_context, _prepare_transaction,
        )
        from attention_router.application.context_pg_consent import PgSourceConsent

        scope = registry.resolve(db, bearer=args["bearer"], scope_ref=scope_ref)
        _prepare_transaction(writer._reader, db, args["bearer"], scope, pg_consent=True)
        _, permits = PgSourceConsent().current(db, scope)
        ctx = _authorized_context(writer._reader, db, args["bearer"], scope, permits)
        token = epoch.token(ctx)
        assert token.epoch == saved.epoch
        assert epoch.checkpoint(ctx) == proof.checkpoint


@pytest.mark.parametrize("lab", ["client-query"], indirect=True)
@pytest.mark.parametrize("prod_canary", [False, True])
def test_owner_enrollment_durable_epoch_e5_http_revocation(
    Session, consent_clients, context_reader, lab, provider, tmp_path, monkeypatch, prod_canary,  # noqa: F811
):
    """Single disposable-DB proof of real OWNER scope, anchor, E5 and HTTP."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from starlette.routing import Route

    from attention_router.api.v1.client_context_staged import StagedContextEndpoint
    from attention_router.application.client_context import ClientContextReader
    from attention_router.application.context_epoch import ContextEpoch
    from attention_router.application.context_owner_epoch import OwnerEpochProvisioner
    from attention_router.application.context_pg_consent import PgSourceConsentWriter, PgSourceConsent
    from attention_router.application.conversation_evidence_staged import StagedConversationEvidenceReader
    from attention_router.application import conversation_evidence_staged, conversation_vectors
    from attention_router.infrastructure.models import ConversationMessageRow

    role, restricted = context_reader
    args, writer, _, decision, _ = consent_clients
    if prod_canary:
        from test_postgres_client_context import enroll_device, issue
        from test_postgres_client_session import NOW
        device = enroll_device(Session)
        first_session = issue(Session, device, NOW, tenant_id=writer._reader._scopes["owner"].tenant_id)
        args = {**args, "bearer": first_session.session_token}
    registry = AuthenticatedOwnerScopeRegistry(
        client_sessions=service(),
        clock=writer._reader._clock,
        auth_clock=writer._reader._auth_clock,
    )

    with writer._reader._sessions.begin() as db:
        scope_ref = registry.enroll(
            db, bearer=args["bearer"], message_id=decision("confirm")["message_id"],
            capture_start=writer._reader._scopes["owner"].start,
        )
    owner_writer = PgSourceConsentWriter(writer._reader, owner_scope_registry=registry)

    # The source corpus is explicitly granted after enrollment. These
    # synthetic media-caption messages must be TEXT to be indexable by E5.
    with Session.begin() as db:
        for name in ("proposal", "confirm", "conflict"):
            row = db.get(ConversationMessageRow, decision(name)["message_id"])
            row.message_type = "TEXT"
    for name in ("proposal", "confirm"):
        assert owner_writer.grant(
            bearer=args["bearer"], scope_ref=scope_ref, **decision(name),
        )

    key = b"synthetic-owner-e5-anchor-key-for-ci-only-32"
    environment = lab[5]._epoch.environment
    provision = OwnerEpochProvisioner(
        reader=writer._reader, owner_scope_registry=registry,
        environment=environment, authority_id="owner-staged-anchor",
        key=key, path=tmp_path / "scope-anchor.sqlite",
    )
    anchor = provision.provision_explicit(
        bearer=args["bearer"], scope_ref=scope_ref, operator_approved=True,
    )
    # Keep indexing inside the disposable lab before switching the canary
    # application to its production-mode configuration.

    # The separate vector worker writes only to the disposable DB.
    for _ in range(40):
        with Session() as db:
            pending = db.scalar(text(
                "SELECT count(*) FROM queue WHERE kind=:kind "
                "AND status IN ('PENDING','PROCESSING')"
            ), {"kind": conversation_vectors.KIND})
        if not pending:
            break
        conversation_vectors.cycle(Session, provider)
    else:
        pytest.fail("SYNTHETIC_INDEX_NOT_DRAINED")

    with Session.begin() as db:
        db.execute(text(
            f"GRANT SELECT ON "
            f"conversation_vector_segments, conversation_vector_memberships, conversation_embeddings "
            f"TO {role}"
        ))

    if prod_canary:
        from attention_router.config import settings
        bound = writer._reader._scopes["owner"]
        canary = {
            "app_env": "production",
            "staged_context_private_canary_enabled": True,
            "client_session_enabled": True,
            "context_epoch_authority_canary_enabled": True,
            "cognitive_runtime_canary_tenant_id": bound.tenant_id,
            "persistent_memory_canary_binding_id": bound.owner_binding_id,
            "staged_context_canary_owner_id": bound.owner_id,
            "staged_context_canary_source_account": bound.source_account,
        }
        for field, value in canary.items():
            monkeypatch.setattr(settings, field, value)
    epoch = ContextEpoch(
        anchor, authority_id="owner-staged-anchor", environment=environment, key=key,
    )

    original = ClientContextReader(
        restricted,
        role=role,
        client_sessions=service(),
        scope_registry={},
        consent=None, epoch=epoch, read=writer._reader._read,
        clock=writer._reader._clock, auth_clock=writer._reader._auth_clock,
    )
    reader = StagedConversationEvidenceReader(
        original, provider=provider, pg_consent=PgSourceConsent(),
        owner_scope_registry=registry, reserve_query=lambda scope: True,
        token_budget=4000,
    )
    from attention_router.web.private_context import QUERY_PATH, query_endpoint

    path = QUERY_PATH if prod_canary else "/api/v1/client/context/staged-evidence"
    endpoint = query_endpoint(original, provider, registry) if prod_canary else StagedContextEndpoint(reader)
    app = FastAPI(routes=[Route(path, endpoint, methods=["POST"])])
    headers = {"Authorization": "Bearer " + args["bearer"]}
    payload = {"scope_ref": scope_ref, "question": "Em que data ficou a viagem?"}

    active_source = "confirm"
    from contextlib import ExitStack
    with ExitStack() as stack:
        if prod_canary:
            import socket
            import threading
            import time
            import httpx
            import uvicorn

            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(
                app, host="127.0.0.1", port=port, access_log=False,
                log_level="critical", lifespan="off",
            ))
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()

            def stop():
                server.should_exit = True
                thread.join(timeout=5)
                assert not thread.is_alive()

            stack.callback(stop)
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.started
            client = stack.enter_context(httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False,
            ))
        else:
            client = stack.enter_context(TestClient(app))
        first = client.post(
            path, headers=headers, json=payload,
        )
        assert first.status_code == 200
        if prod_canary:
            assert first.json()["kind"] in ("OBSERVATION", "INSUFFICIENT_EVIDENCE")
            assert first.json()["correlation_id"] == first.headers["x-correlation-id"]
            quoted = client.post(path, headers=headers, json={**payload, "question": "Confirmo a viagem para domingo."})
            assert quoted.status_code == 200 and decision("confirm")["message_id"] in {
                entry["source_ref"] for entry in quoted.json()["evidence"]
            }

            import json
            import os
            from pathlib import Path
            import subprocess
            import sys
            from attention_router.infrastructure.client_session_models import ClientSessionRow

            # Persisted scope, consent and independent epoch survive both a new
            # ClientSession for the same physical key and a new reader process.
            with Session.begin() as db:
                closed = db.get(ClientSessionRow, first_session.session_id)
                closed.state = "REVOKED"
                closed.revoked_at = NOW
            old = client.post(path, headers=headers, json=payload)
            assert old.status_code != 200 and old.content == b""
            new_session = issue(Session, device, NOW, tenant_id=bound.tenant_id)
            assert new_session.session_id != first_session.session_id
            args = {"bearer": new_session.session_token, "scope_ref": scope_ref}
            headers = {"Authorization": "Bearer " + args["bearer"]}
            known = {**payload, "question": "Confirmo a viagem para domingo."}
            child = subprocess.run(
                [sys.executable, "-m", "andy_reopen_private_context"],
                input=json.dumps({
                    "database": restricted.kw["bind"].url.render_as_string(hide_password=False),
                    "canary": canary, "clock": original._clock().isoformat(),
                    "auth_clock": original._auth_clock().isoformat(), "key": key.hex(),
                    "authority_id": "owner-staged-anchor", "environment": environment,
                    "anchor": str(tmp_path / "scope-anchor.sqlite"), "bearer": args["bearer"],
                    "question": known,
                }), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45,
                cwd=Path(__file__).resolve().parents[2],
                env={**os.environ, "PYTHONPATH": os.pathsep.join((str(Path(__file__).parent), str(Path.cwd())))},
            )
            assert child.returncode == 0, "new synthetic reader failed"
            reopened, earlier = json.loads(child.stdout), quoted.json()
            assert reopened.pop("correlation_id") != earlier.pop("correlation_id")
            assert reopened == earlier

            # An explicit correction withdraws the old source and consents to
            # its distinct replacement receipt. The original tombstone persists.
            superseded = decision("confirm")
            replacement = decision("conflict")
            assert superseded["receipt_id"] != replacement["receipt_id"]
            assert owner_writer.revoke_current(**args, message_id=superseded["message_id"])
            assert owner_writer.grant(
                **args, **{**replacement, "decision_ref": "fixture:owner-corrected-travel-date"},
            )
            payload = {**payload, "question": "Confirmo a viagem para sábado."}
            corrected = client.post(path, headers=headers, json=payload)
            assert corrected.status_code == 200
            cited = {e["source_ref"] for e in corrected.json()["evidence"]}
            assert replacement["message_id"] in cited and superseded["message_id"] not in cited
            active_source = "conflict"
        else:
            assert first.json()["items"]

        original_compile = conversation_evidence_staged._compile

        def withdraw_in_compute(*call_args, **call_kwargs):
            calculated = original_compile(*call_args, **call_kwargs)
            assert owner_writer.revoke(
                bearer=args["bearer"], scope_ref=scope_ref,
                message_id=decision(active_source)["message_id"],
            )
            return calculated

        monkeypatch.setattr(
            conversation_evidence_staged, "_compile", withdraw_in_compute,
        )
        refused = client.post(
            path, headers=headers, json=payload,
        )
        assert refused.status_code == 503 and refused.content == b""
