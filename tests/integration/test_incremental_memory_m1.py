"""Real session proof and memory delivery, exclusively in disposable CI PostgreSQL.

Never requests pg_url/Session: those fixtures migrate the former cognitive schema.
No HTTP sockets, production DBs, external providers, or M0 resources are used.
"""

import base64
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import os
import secrets
from types import SimpleNamespace
import uuid

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from experiments.incremental_memory import memory
from experiments.incremental_memory.authenticated import reader
from experiments.incremental_memory.authenticated.app import (
    PATH,
    create_app,
    require_experimental,
)
from experiments.incremental_memory.authenticated.schema import (
    create_disposable_schema,
    grant_runtime_roles,
    register_scope,
)
from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.models import TenantRow
from attention_router.infrastructure.client_bootstrap_models import (
    ClientDeviceRow,
    ClientTenantMembershipRow,
)

pytestmark = pytest.mark.postgres


class Lab(SimpleNamespace):
    def __repr__(self):
        return "<SyntheticM1Lab: credentials redacted>"


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


@pytest.fixture()
def lab(monkeypatch):
    raw = os.environ.get("PUBLIC_POSTGRES_TEST_URL")
    if not raw:
        pytest.skip("Disposable worker PostgreSQL required")
    url = make_url(raw)
    if (
        url.host not in {"postgres", "localhost", "127.0.0.1"}
        or url.username != "ar_test"
        or url.database != "attention_router_test"
        or url.query
    ):
        pytest.fail("Disposable CI database required")
    db_name = "andy_memory_m1_" + uuid.uuid4().hex
    issuer_role, reader_role = "m1_i_" + uuid.uuid4().hex, "m1_r_" + uuid.uuid4().hex
    issuer_password, reader_password = (
        secrets.token_urlsafe(32),
        secrets.token_urlsafe(32),
    )
    cluster_url = url.set(
        drivername="postgresql", database="postgres"
    ).render_as_string(hide_password=False)
    engines = []
    roles_created = []
    database_created = False
    with psycopg.connect(cluster_url, autocommit=True) as cluster:
        try:
            cluster.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name))
            )
            database_created = True
            for role, password in (
                (issuer_role, issuer_password),
                (reader_role, reader_password),
            ):
                cluster.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
                roles_created.append(role)
            local_url = url.set(database=db_name)
            engine = create_engine(local_url, hide_parameters=True)
            engines.append(engine)
            create_disposable_schema(engine)
            with engine.begin() as conn:
                grant_runtime_roles(conn, issuer_role, reader_role)
            admin = sessionmaker(bind=engine, expire_on_commit=False)
            factories = []
            for role, password in (
                (issuer_role, issuer_password),
                (reader_role, reader_password),
            ):
                runtime_engine = create_engine(
                    local_url.set(username=role, password=password),
                    hide_parameters=True,
                    connect_args={"connect_timeout": 3},
                    pool_size=2,
                    max_overflow=0,
                )
                engines.append(runtime_engine)
                factories.append(
                    sessionmaker(bind=runtime_engine, expire_on_commit=False)
                )
            private = ec.generate_private_key(ec.SECP256R1())
            spki = private.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            human, device = "hid_" + uuid.uuid4().hex, "cdev_" + uuid.uuid4().hex
            tenant, other_tenant = "m1-tenant-one", "m1-tenant-two"
            now = datetime.now(UTC)
            with admin.begin() as s:
                s.add(HumanIdentityRow(id=human, created_at=now))
                for tid in (tenant, other_tenant):
                    s.add(
                        TenantRow(
                            id=tid,
                            slug=tid,
                            name="Synthetic tenant",
                            status="ACTIVE",
                            created_at=now,
                            updated_at=now,
                        )
                    )
                s.flush()
                s.add(
                    ClientDeviceRow(
                        id=device,
                        human_identity_id=human,
                        public_key_fingerprint="sha256:"
                        + hashlib.sha256(spki).hexdigest(),
                        public_key_spki=spki,
                        canonical_name="Synthetic enrolled Android",
                        platform="ANDROID",
                        roles=["CLIENT"],
                        status="ACTIVE",
                        created_at=now,
                        updated_at=now,
                    )
                )
                for tid in (tenant, other_tenant):
                    s.add(
                        ClientTenantMembershipRow(
                            id="cmem_" + uuid.uuid4().hex,
                            human_identity_id=human,
                            tenant_id=tid,
                            role="OWNER",
                            status="ACTIVE",
                            created_at=now,
                            updated_at=now,
                        )
                    )
            memory_url = local_url.set(drivername="postgresql").render_as_string(
                hide_password=False
            )

            def connect(write=False):
                return psycopg.connect(memory_url, row_factory=dict_row)

            monkeypatch.setattr(memory, "connect", connect)
            recorded = memory.register(
                "Roberto", "vehicle.color", "azul", "Roberto possui um veículo azul."
            )
            other = memory.register(
                "Nina", "employment.company", "Beta", "Nina trabalha na empresa Beta."
            )
            with admin.begin() as s:
                scope = register_scope(
                    s,
                    owner_id=human,
                    tenant_id=tenant,
                    information_ids=[recorded["information_id"]],
                    expires_at=now + timedelta(hours=1),
                )
                other_scope = register_scope(
                    s,
                    owner_id=human,
                    tenant_id=other_tenant,
                    information_ids=[other["information_id"]],
                    expires_at=now + timedelta(hours=1),
                )
            settings = SimpleNamespace(
                client_session_enabled=True,
                client_session_challenge_ttl_seconds=60,
                client_session_ttl_seconds=300,
            )
            app = create_app(
                issuer_factory=factories[0],
                reader_factory=factories[1],
                settings=settings,
            )
            with TestClient(app) as client:
                challenge = client.post(
                    "/api/v1/session/device/challenges",
                    json={
                        "public_key_spki_b64url": b64(spki),
                        "requested_tenant_id": tenant,
                    },
                )
                assert challenge.status_code == 201
                body = challenge.json()
                raw_challenge = base64.urlsafe_b64decode(
                    body["challenge_b64url"] + "=="
                )
                signature = private.sign(raw_challenge, ec.ECDSA(hashes.SHA256()))
                issued = client.post(
                    "/api/v1/session/device/challenges/"
                    + body["session_challenge_id"]
                    + "/complete",
                    json={"device_signature_b64url": b64(signature)},
                )
                assert issued.status_code == 200
                bearer = issued.json()["session"]["session_token"]
                endpoint = next(
                    r.endpoint for r in app.routes if getattr(r, "path", None) == PATH
                )
                yield Lab(
                    client=client,
                    admin=admin,
                    scope=scope,
                    other_scope=other_scope,
                    bearer=bearer,
                    human=human,
                    device=device,
                    tenant=tenant,
                    recorded=recorded,
                    reader_factory=factories[1],
                    issuer_factory=factories[0],
                    delivery=endpoint.delivery,
                    endpoint=endpoint,
                    settings=settings,
                )
        finally:
            for engine in reversed(engines):
                engine.dispose()
            if database_created:
                cluster.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                        sql.Identifier(db_name)
                    )
                )
            for role in roles_created:
                cluster.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def query(
    lab,
    question="Qual é a cor do carro de Roberto?",
    *,
    token=None,
    scope=None,
    **extra,
):
    return lab.client.post(
        PATH,
        headers={"Authorization": "Bearer " + (token or lab.bearer)},
        json={"scope_ref": scope or lab.scope, "question": question, **extra},
    )


def test_authenticated_persisted_answer(lab):
    bootstrap = lab.client.get(
        "/api/v1/client/bootstrap", headers={"Authorization": "Bearer " + lab.bearer}
    )
    assert bootstrap.status_code == 200
    response = query(lab)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["status"] == "ANSWERED"
    assert result["answer"] == "O carro de Roberto é azul."
    assert result["evidence"][0]["evidence_id"] == lab.recorded["evidence_id"]
    assert result["evidence"][0]["revision"] == 1
    assert result["correlation_id"] == response.headers["x-correlation-id"]
    with lab.admin.begin() as s:
        assert s.execute(text("SELECT count(*) FROM client_sessions")).scalar_one() == 1
        assert (
            s.execute(text("SELECT token_digest FROM client_sessions")).scalar_one()
            == hashlib.sha256(lab.bearer.encode()).hexdigest()
        )


@pytest.mark.parametrize("invalid", ["invalid", "cst_" + "X" * 43])
def test_invalid_session(lab, invalid):
    response = query(lab, token=invalid)
    assert response.status_code == 401
    assert response.json()["evidence"] == []


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE client_sessions SET expires_at=now()-interval '1 second',created_at=now()-interval '1 hour'",
        "UPDATE client_sessions SET state='REVOKED',revoked_at=now()",
        "UPDATE client_devices SET status='REVOKED'",
        "UPDATE client_tenant_memberships SET status='REVOKED'",
        "UPDATE client_tenant_memberships SET role='MEMBER'",
        "UPDATE memory_scopes SET active=false",
        "UPDATE memory_scopes SET expires_at=now()-interval '1 second'",
    ],
)
def test_expiration_revocation_and_owner(lab, mutation):
    with lab.admin.begin() as s:
        s.execute(text(mutation))
    response = query(lab)
    assert response.status_code in (401, 403)
    assert response.json()["evidence"] == []


def test_tenant_scope_and_caller_assertions(lab):
    assert query(lab, scope=lab.other_scope).status_code == 403
    assert query(lab, scope="ctx_" + "0" * 64).status_code == 403
    assert query(lab, tenant_id=lab.tenant).status_code == 422
    assert query(lab, owner_id=lab.human).status_code == 422
    assert (
        lab.client.post(
            PATH, json={"scope_ref": lab.scope, "question": "Onde Nina trabalha?"}
        ).status_code
        == 401
    )


def test_scope_filters_abstention_and_current_revision(lab):
    assert query(lab, "Onde Nina trabalha?").json()["status"] == "INSUFFICIENT_EVIDENCE"
    assert (
        query(lab, "Onde Roberto trabalha?").json()["status"] == "INSUFFICIENT_EVIDENCE"
    )
    corrected = memory.register(
        "Roberto", "vehicle.color", "branco", "Correção: veículo branco.", expected=1
    )
    result = query(lab, "De que cor é o veículo de Roberto?").json()
    assert result["answer"] == "O carro de Roberto é branco."
    assert result["evidence"][0]["evidence_id"] == corrected["evidence_id"]
    assert result["evidence"][0]["revision"] == 2
    assert [r["value"] for r in memory.retrieve("Roberto", "vehicle.color", True)] == [
        "azul",
        "branco",
    ]


@pytest.mark.parametrize(
    "change,expected", [("revision", 409), ("scope", 403), ("session", 401)]
)
def test_change_between_capture_and_delivery(lab, monkeypatch, change, expected):
    present = reader.present

    def concurrently_changed(rows, correlation):
        if change == "revision":
            memory.register(
                "Roberto",
                "vehicle.color",
                "branco",
                "Correção: veículo branco.",
                expected=1,
            )
        else:
            with lab.admin.begin() as s:
                s.execute(
                    text(
                        "UPDATE memory_scopes SET active=false"
                        if change == "scope"
                        else "UPDATE client_sessions SET state='REVOKED',revoked_at=now()"
                    )
                )
        return present(rows, correlation)

    monkeypatch.setattr(reader, "present", concurrently_changed)
    response = query(lab)
    assert response.status_code == expected
    assert response.json()["evidence"] == []
    assert "azul" not in response.text


def test_actual_delivery_holds_write_fence_and_releases(lab):
    bodies = []

    def deliver(body):
        with pytest.raises(OperationalError):
            with lab.admin.begin() as competing:
                competing.execute(text("SET LOCAL lock_timeout='100ms'"))
                competing.execute(text("UPDATE memory_scopes SET active=false"))
        bodies.append(body)

    lab.delivery.query_and_deliver(
        bearer=lab.bearer,
        scope_ref=lab.scope,
        question="Qual é a cor do carro de Roberto?",
        deliver=deliver,
    )
    assert len(bodies) == 1
    with lab.admin.begin() as s:
        s.execute(text("UPDATE memory_scopes SET active=false"))
    assert query(lab).status_code == 403


def test_reader_cannot_write_memory_or_identity(lab):
    for statement in (
        "UPDATE information SET current_revision=2",
        "UPDATE client_devices SET status='REVOKED'",
        "DELETE FROM memory_scope_information",
    ):
        with pytest.raises(Exception) as caught:
            with lab.reader_factory.begin() as s:
                s.execute(text(statement))
        assert getattr(caught.value.orig, "sqlstate", None) == "42501"
    assert require_experimental(lab.reader_factory, reader=True)[1].startswith("m1_r_")


def test_schema_fence_required_and_no_previous_migrations(lab):
    with lab.admin.begin() as s:
        names = set(
            s.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            ).scalars()
        )
        assert len(names) == 12
        assert not names & {
            "alembic_version",
            "outbox",
            "conversation_segments",
            "memory_embeddings",
        }
        s.execute(text("ALTER TABLE information DISABLE TRIGGER memory_m1_write_fence"))
    with pytest.raises(ValueError, match="WRITE_FENCES_REQUIRED"):
        require_experimental(lab.reader_factory, reader=True)


def test_infrastructure_failure_is_not_abstention(lab, monkeypatch):
    @contextmanager
    def unavailable():
        raise OperationalError("redacted", {}, Exception("synthetic unavailable"))
        yield

    monkeypatch.setattr(lab.reader_factory, "begin", unavailable)
    response = query(lab)
    assert response.status_code == 503
    assert response.json()["status"] == "UNAVAILABLE"
    assert response.json()["evidence"] == []


def test_query_bounds_and_no_write_routes(lab):
    assert query(lab, " " * 10).status_code == 422
    assert query(lab, "x" * 1001).status_code == 422
    assert lab.client.post("/register", json={}).status_code == 404
    assert lab.client.get(PATH).status_code == 405
