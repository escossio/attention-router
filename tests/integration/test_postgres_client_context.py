"""One isolated authenticated flow across durable context, correction and withdrawal."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
from secrets import token_hex
import subprocess
import sys
from threading import Event
from time import monotonic, sleep

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from andy_client_context import QUERY_PATH, app, read
from attention_router.application.candidate_insights import reject_candidate_insight
from attention_router.application.context_validity import ContextValidity
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.client_bootstrap_models import ClientDeviceRow, ClientTenantMembershipRow
from attention_router.infrastructure.client_session_models import ClientSessionRow
from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.models import ConversationMessageRow
from test_postgres_client_session import NOW, keypair, service, sign
from test_postgres_cognitive_lab import enroll_role, lab  # noqa: F401

pytestmark = pytest.mark.postgres
LOCK_TABLES = (
    "actor_bindings", "artifacts", "artifact_receipts", "candidate_insight_evidence", "candidate_insights",
    "canonical_events", "client_devices", "client_sessions", "client_tenant_memberships",
    "conversation_messages", "conversation_threads", "inbound_events", "resources",
    "semantic_episodes", "tenants", "timeline_events", "voice_transcriptions",
)
TENANT_READ_TABLES = ("audit_events", "memory_actors", "relationships", "semantic_episode_memberships")
READ_TABLES = (*LOCK_TABLES, *TENANT_READ_TABLES, "alembic_version")
QUESTION = "Em que data ficou a viagem?"


@pytest.fixture
def context_reader(Session, lab):  # noqa: F811
    role = "fixture_context_" + token_hex(6)
    password = token_hex(24)
    tenant = DEFAULT_TENANT_ID
    writer_role = lab[5]._role
    account = lab[6]["source_account"]
    newly_secured = []
    # UPDATE(id) is the narrow PostgreSQL privilege for row locks. RLS's
    # WITH CHECK denies every change, including a no-op assignment.
    with Session.begin() as admin:
        admin.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"))
        admin.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        admin.execute(text(f"GRANT SELECT ON {', '.join(READ_TABLES)} TO {role}"))
        admin.execute(text(
            f"GRANT EXECUTE ON FUNCTION public.andy_context_index_lock(text,boolean) TO {role}"
        ))
        for table in (*LOCK_TABLES, *TENANT_READ_TABLES):
            allowed = (
                f"tenant_id = '{tenant}'" if table != "tenants" else f"id = '{tenant}'"
            )
            if table == "client_devices":
                allowed = "human_identity_id = 'fixture-human-owner'"
            if table in {"conversation_messages", "conversation_threads", "artifact_receipts"}:
                allowed += f" AND source_account = '{account}'"
            enabled = admin.scalar(text("SELECT relrowsecurity FROM pg_class WHERE oid=CAST(:table AS regclass)"),
                                   {"table": table})
            if not enabled:
                newly_secured.append(table)
                admin.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            admin.execute(text(f"CREATE POLICY client_context_writer ON {table} FOR ALL TO {writer_role} "
                               "USING (true) WITH CHECK (true)"))
            admin.execute(text(f"CREATE POLICY client_context_select ON {table} FOR SELECT TO {role} USING ({allowed})"))
            if table in LOCK_TABLES:
                admin.execute(text(f"CREATE POLICY client_context_lock ON {table} FOR UPDATE TO {role} "
                                   f"USING ({allowed}) WITH CHECK (false)"))
                admin.execute(text(f"GRANT UPDATE(id) ON {table} TO {role}"))
    engine = create_engine(
        Session.kw["bind"].url.set(username=role, password=password), hide_parameters=True,
    )
    try:
        yield role, sessionmaker(engine, autoflush=False)
    finally:
        engine.dispose()
        with Session.begin() as admin:
            for table in (*LOCK_TABLES, *TENANT_READ_TABLES):
                admin.execute(text(f"DROP POLICY client_context_select ON {table}"))
                if table in LOCK_TABLES:
                    admin.execute(text(f"DROP POLICY client_context_lock ON {table}"))
                admin.execute(text(f"DROP POLICY client_context_writer ON {table}"))
            for table in newly_secured:
                admin.execute(text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
            admin.execute(text(f"DROP OWNED BY {role}"))
            admin.execute(text(f"DROP ROLE {role}"))


def enroll_device(Session, human_id="fixture-human-owner"):
    private, spki, public = keypair()
    with Session.begin() as session:
        session.add(ClientDeviceRow(
            id="cdev_" + token_hex(12), human_identity_id=human_id, public_key_spki=spki,
            public_key_fingerprint="sha256:" + hashlib.sha256(spki).hexdigest(),
            canonical_name="Synthetic Android device", platform="ANDROID", roles=["CLIENT"],
            status="ACTIVE", created_at=NOW, updated_at=NOW,
        ))
    return private, public


def issue(Session, device, stamp, *, tenant_id=None):
    private, public = device
    with Session.begin() as session:
        challenge = service().start_session(
            session, public_key_spki_b64url=public, requested_tenant_id=tenant_id, now=stamp,
        )
    with Session.begin() as session:
        return service().complete_session(
            session, session_challenge_id=challenge.session_challenge_id,
            device_signature_b64url=sign(private, challenge.challenge_b64url), now=stamp,
        )


def ask(client, issued, scope, *, payload=None, question=QUESTION):
    return client.post(
        QUERY_PATH, json={"scope_ref": scope, "question": question, **(payload or {})},
        headers={"Authorization": "Bearer " + issued.session_token} if issued else {},
    )


@pytest.mark.parametrize("lab", ["client-query"], indirect=True)
def test_authenticated_context_query_across_processes(Session, lab, context_reader, caplog):  # noqa: F811
    caplog.set_level(logging.INFO, logger="attention_router.api.v1.client_context")
    ids, clock, observe, _, change, pilot, scope, consent, no_effects, config = lab
    role, restricted = context_reader
    scope_ref = "ctx_" + token_hex(24)
    auth_now = NOW
    device = enroll_device(Session)
    first = issue(Session, device, auth_now)
    other_human = "hid_" + token_hex(12)
    with Session.begin() as session:
        session.add(HumanIdentityRow(id=other_human, created_at=NOW))
        session.flush()
        session.add(ClientTenantMembershipRow(
            id="ctm_" + token_hex(12), human_identity_id=other_human, tenant_id=scope["tenant_id"],
            role="MEMBER", status="ACTIVE", created_at=NOW, updated_at=NOW,
        ))
        session.add(ClientTenantMembershipRow(
            id="ctm_" + token_hex(12), human_identity_id=scope["owner_human_identity_id"],
            tenant_id="other", role="OWNER", status="ACTIVE", created_at=NOW, updated_at=NOW,
        ))
    foreign_tenant = issue(Session, device, auth_now, tenant_id="other")
    member_device = enroll_device(Session, other_human)
    non_owner = issue(Session, member_device, auth_now, tenant_id=scope["tenant_id"])

    def new_app(*, read_fn=read):
        return app(restricted, role, consent, pilot._epoch, scope, scope_ref, config()["resource_id"],
                   lambda: pilot._clock(), lambda: auth_now, read_fn=read_fn)

    observe("plan")
    clock(day=2)
    observe("proposal")
    clock(day=3)
    observe("confirm")
    with TestClient(new_app()) as client:
        assert ask(client, None, scope_ref).status_code == 401
        assert ask(client, first, "ctx_" + token_hex(24)).status_code == 403
        assert ask(client, foreign_tenant, scope_ref).status_code == 401
        assert ask(client, non_owner, scope_ref).status_code == 403
        rejected = ask(client, first, scope_ref, payload={"tenant_id": "other", "source_account": "other",
                                                         "role": "OWNER", "owner_id": "forged"})
        assert rejected.status_code == 422
        assert rejected.json() == {"code": "CLIENT_CONTEXT_QUERY_INVALID"}
        initial = ask(client, first, scope_ref)
        assert initial.status_code == 200, initial.json()
        assert initial.headers["Cache-Control"] == "no-store"
        initial = initial.json()
        assert f"correlation={initial['correlation_id']} state=OBSERVATION" in caplog.text
        assert first.session_token not in caplog.text and QUESTION not in caplog.text
        assert ids["confirm"] not in caplog.text
        assert initial["kind"] == "OBSERVATION" and initial["date"] == "2026-11-29"
        assert {source["source_ref"] for source in initial["evidence"]} == {ids["proposal"], ids["confirm"]}
        assert ask(client, first, scope_ref, question="Qual o endereço da viagem?").json()["kind"] == "INSUFFICIENT_EVIDENCE"
        assert ask(client, first, scope_ref, question="histórico").json()["evidence"] == []

    auth_now += timedelta(seconds=901)
    with TestClient(new_app()) as client:
        assert ask(client, first, scope_ref).status_code == 401
    with Session.begin() as writer:
        expired = writer.get(ClientSessionRow, first.session_id)
        expired.state = "REVOKED"
        expired.revoked_at = auth_now
    second = issue(Session, device, auth_now, tenant_id=scope["tenant_id"])
    assert second.session_id != first.session_id
    child = subprocess.run(
        [sys.executable, "-m", "andy_lab"],
        input=json.dumps({
            **config(), "view": "client-query", "url": restricted.kw["bind"].url.render_as_string(hide_password=False),
            "scope_ref": scope_ref, "question": QUESTION, "auth_now": auth_now.isoformat(), "bearer": second.session_token,
        }, default=lambda value: value.isoformat()),
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(Path(__file__).parent), str(Path.cwd())))},
        text=True, stdout=subprocess.PIPE, timeout=30, check=True,
    )
    reopened = json.loads(child.stdout)
    assert reopened["status"] == 200
    assert reopened["body"].pop("correlation_id") != initial.pop("correlation_id")
    assert reopened["body"] == initial

    corrected, _ = observe("conflict")

    def correct(context):
        reject_candidate_insight(
            context.session, candidate_id=corrected, decision_actor_key=context.binding.actor_key,
            decision_ref="fixture:context-client-owner-correction", now=context.arguments["now"],
        )
        context._advance("LAB_OWNER_CORRECTION")

    with TestClient(new_app()) as client:
        conflict = ask(client, second, scope_ref).json()
        assert conflict["kind"] == "CONFLICT" and ids["conflict"] in {e["source_ref"] for e in conflict["evidence"]}
        change(correct)
        fixed = ask(client, second, scope_ref).json()
        assert fixed["kind"] == "OBSERVATION" and ids["conflict"] not in {e["source_ref"] for e in fixed["evidence"]}
        assert observe("conflict")[0] == corrected

    answered, release, withdrawal_started = Event(), Event(), Event()
    writer_pid = []
    with consent.current() as (_, permits):
        withdrawal_permits = permits

    def hold_reader(*args):
        value = read(*args)
        answered.set()
        assert release.wait(8)
        return value

    def withdraw():
        with Session.begin() as writer:
            writer_pid.append(writer.scalar(text("SELECT pg_backend_pid()")))
            withdrawal_started.set()
            context = ContextValidity(writer, **scope, now=pilot._clock(), source_allowed=withdrawal_permits)
            return pilot._epoch.advance(context, context.revoke_message, ids["confirm"])

    with ThreadPoolExecutor(max_workers=2) as workers, TestClient(new_app(read_fn=hold_reader)) as client:
        before = workers.submit(ask, client, second, scope_ref)
        try:
            assert answered.wait(8)
            withdrawal = workers.submit(withdraw)
            assert withdrawal_started.wait(8)
            with Session() as monitor:
                timeout = monotonic() + 3
                while not monitor.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": writer_pid[0]}):
                    assert monotonic() < timeout and not withdrawal.done()
                    sleep(0.025)
        finally:
            release.set()
        assert before.result(timeout=15).json()["kind"] == "OBSERVATION"
        withdrawal.result(timeout=15)

    with TestClient(new_app()) as client:
        after = ask(client, second, scope_ref).json()
        assert after["kind"] == "HYPOTHESIS"
        assert {e["source_ref"] for e in after["evidence"]} == {ids["proposal"]}
        consent.revoke(ids["proposal"])
        redacted = ask(client, second, scope_ref).json()
        assert redacted["kind"] == "HYPOTHESIS"
        assert {e["source_ref"] for e in redacted["evidence"]} == {ids["plan"]}
        assert ids["confirm"] not in json.dumps(redacted)
        assert ids["proposal"] not in json.dumps(redacted)
        with Session.begin() as writer:
            session_row = writer.get(ClientSessionRow, second.session_id)
            session_row.state = "REVOKED"
            session_row.revoked_at = auth_now
        assert ask(client, second, scope_ref).status_code == 401
        third = issue(Session, device, auth_now, tenant_id=scope["tenant_id"])
        with Session.begin() as writer:
            writer.get(ClientDeviceRow, third.device_id).status = "REVOKED"
        assert ask(client, third, scope_ref).status_code == 403

    with restricted() as session:
        assert not session.scalars(select(ConversationMessageRow.id).where(
            ConversationMessageRow.id.in_((ids["other_tenant"], ids["other_account"])))).all()

    for sql, expected in (
        ("UPDATE conversation_messages SET id=id WHERE id=:id", "42501"),
        ("UPDATE conversation_messages SET searchable=false WHERE id=:id", "42501"),
        ("SELECT id FROM outbox_messages LIMIT 1", "42501"),
        ("SELECT id FROM execution_intents LIMIT 1", "42501"),
        ("SELECT id FROM capability_grants LIMIT 1", "42501"),
    ):
        with restricted() as session, pytest.raises(DBAPIError) as prohibited:
            session.execute(text(sql), {"id": ids["plan"]})
        assert prohibited.value.orig.sqlstate == expected, sql
    no_effects()
    print("andy_client_context authenticated, process_restart, owner_correction, source_withdrawal, "
          "postgres_lock, consent, tenant, account, identity, expiry, device, audit, no_effects")
