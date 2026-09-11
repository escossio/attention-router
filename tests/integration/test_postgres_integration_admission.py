"""Committed PostgreSQL state and real concurrent connections; synthetic data only."""
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import secrets
import threading
import time

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError, DBAPIError

from attention_router.infrastructure.models import (
    IntegrationBindingRow as BindingRow, IntegrationCredentialRow as CredentialRow,
    IntegrationInboxRow as InboxRow, TenantRow,
)
from attention_router.integrations.admission import (
    admit_inbound, disable_binding, provision_binding, provision_credential, revoke_credential,
)
from attention_router.integrations.tenant_binding import (
    INBOUND_SCOPE, IntegrationBinding, CredentialRecord, credential_digest,
)

pytestmark = pytest.mark.postgres
A = "00000000-0000-4000-8000-000000000001"
B = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
def world(Session):
    now = datetime.now(timezone.utc)
    with Session.begin() as session:
        for identity in (A, B):
            if session.get(TenantRow, identity) is not None:
                continue
            session.add(TenantRow(id=identity, slug=identity, name="Synthetic", status="ACTIVE",
                                  created_at=now, updated_at=now))
    binding = IntegrationBinding("binding-a", "test-ingress", A, "CHANNEL", "channel.email",
                                 "mailbox-1", None, True, frozenset({INBOUND_SCOPE}))
    token = secrets.token_urlsafe(32)
    credential = CredentialRecord("credential-a", credential_digest(token), binding.binding_id,
                                  now - timedelta(minutes=1), now + timedelta(hours=1), False,
                                  binding.scopes)
    provision_binding(Session, binding)
    provision_credential(Session, credential)
    payload = {
        "contract_type": "inbound_event", "schema_version": "1", "tenant_id": A,
        "source": {"kind": "CHANNEL", "name": "channel.email", "instance_id": "mailbox-1",
                   "account_id": None},
        "external_event_id": "event-1", "event_type": "message", "payload_type": "REFERENCE",
        "payload_ref": {}, "artifact_ids": [], "occurred_at": now.isoformat(),
        "received_at": now.isoformat(), "idempotency_key": "event-1", "correlation_id": "corr-1",
    }
    return binding, credential, token, payload


def send(Session, world, payload=None, token=None, raw=None):
    return admit_inbound(Session, token or world[2],
                         raw if raw is not None else json.dumps(payload or world[3]).encode(),
                         audience="test-ingress")


def count(Session):
    with Session() as session:
        return session.scalar(select(func.count()).select_from(InboxRow))


def test_commit_retry_rotation_and_revocation(Session, world):
    first = send(Session, world)
    assert first.code == "ACCEPTED"
    with Session() as session:
        row = session.get(InboxRow, first.receipt_id)
        assert row.state == "PENDING" and row.raw_body == json.dumps(world[3]).encode()
        assert row.credential_id == world[1].credential_id
    # A lost response is recovered through a new connection and the original receipt.
    again = send(Session, world)
    assert replace(again, code="ACCEPTED") == first
    token = secrets.token_urlsafe(32)
    provision_credential(Session, replace(world[1], credential_id="rotated",
                                          digest=credential_digest(token)))
    revoke_credential(Session, world[1].credential_id)
    assert send(Session, world).code == "UNAUTHENTICATED"
    assert replace(send(Session, world, token=token), code="ACCEPTED") == first
    assert count(Session) == 1


@pytest.mark.parametrize("change", ["bytes", "key", "event", "payload"])
def test_conflict_is_not_an_ack(Session, world, change):
    assert send(Session, world).code == "ACCEPTED"
    payload = copy.deepcopy(world[3])
    if change == "bytes":
        result = send(Session, world, raw=json.dumps(payload, indent=2).encode())
    else:
        key = {"key": "idempotency_key", "event": "external_event_id", "payload": "payload_ref"}[change]
        payload[key] = {"changed": True} if change == "payload" else "event-2"
        result = send(Session, world, payload)
    assert result.code == "IDEMPOTENCY_CONFLICT" and result.receipt_id is None
    assert count(Session) == 1


@pytest.mark.parametrize("field,value", [("tenant_id", B), ("account_id", "another"),
                                          ("instance_id", "another")])
def test_cross_namespace_denied(Session, world, field, value):
    payload = copy.deepcopy(world[3])
    (payload if field == "tenant_id" else payload["source"])[field] = value
    assert send(Session, world, payload).code == "BINDING_FORBIDDEN"
    assert count(Session) == 0


@pytest.mark.parametrize("body,code", [
    (b'{"x":1,"x":2}', "INVALID_REQUEST"), (b'{', "INVALID_REQUEST"),
    (b'{}', "INVALID_CONTRACT"), (b'NaN', "INVALID_REQUEST"),
    (b'1e999', "INVALID_REQUEST"), (b'\xff', "INVALID_REQUEST"),
    (b' ' * 65537, "BODY_TOO_LARGE"),
])
def test_private_parse_errors_and_no_write(Session, world, body, code):
    assert send(Session, world, raw=body).code == code
    assert send(Session, world, raw=body, token=secrets.token_urlsafe(32)).code == "UNAUTHENTICATED"
    assert count(Session) == 0


def test_full_contract_required(Session, world):
    payload = copy.deepcopy(world[3])
    del payload["correlation_id"]
    assert send(Session, world, payload).code == "INVALID_CONTRACT"
    assert count(Session) == 0


@pytest.mark.parametrize("conflict", [False, True])
def test_simultaneous_admissions(Session, world, conflict):
    barrier = threading.Barrier(2)
    def run(index):
        payload = copy.deepcopy(world[3])
        if conflict and index:
            payload["payload_ref"] = {"variant": 1}
        barrier.wait(timeout=5)
        return send(Session, world, payload)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, range(2)))
    assert sorted(r.code for r in results) == sorted([
        "ACCEPTED", "IDEMPOTENCY_CONFLICT" if conflict else "DUPLICATE"])
    assert count(Session) == 1
    if not conflict:
        assert results[0].receipt_id == results[1].receipt_id


def wait_for_waiter(Session, blocker):
    deadline = time.monotonic() + 1.5
    with Session() as observer:
        while time.monotonic() < deadline:
            if observer.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                    "WHERE :pid = ANY(pg_blocking_pids(pid)))"), {"pid": blocker}):
                return
            time.sleep(0.01)
    pytest.fail("Admission did not reach the real PostgreSQL lock")


@pytest.mark.parametrize("disable", ["credential", "binding", "tenant"])
def test_revocation_commits_before_waiting_admission(Session, world, disable):
    with ThreadPoolExecutor(1) as pool:
        with Session.begin() as blocker:
            blocker.execute(select(TenantRow).where(TenantRow.id == A).with_for_update())
            pid = blocker.scalar(text("SELECT pg_backend_pid()"))
            if disable == "credential":
                blocker.get(CredentialRow, world[1].credential_id).revoked = True
            elif disable == "binding":
                blocker.get(BindingRow, world[0].binding_id).active = False
            else:
                blocker.get(TenantRow, A).status = "INACTIVE"
            future = pool.submit(send, Session, world)
            wait_for_waiter(Session, pid)
        result = future.result(timeout=5)
    assert result.code == ("UNAUTHENTICATED" if disable == "credential" else "BINDING_FORBIDDEN")
    assert count(Session) == 0


def test_admission_commits_before_revocation(Session, world):
    reached = threading.Event()
    release = threading.Event()
    engine = Session.kw["bind"]
    def hold(conn, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO integration_inbox"):
            reached.set()
            assert release.wait(5)
    event.listen(engine, "after_cursor_execute", hold)
    try:
        with ThreadPoolExecutor(2) as pool:
            admission = pool.submit(send, Session, world)
            assert reached.wait(5)
            revocation = pool.submit(revoke_credential, Session, world[1].credential_id)
            with Session() as observer:
                pid = observer.scalar(text("SELECT pid FROM pg_stat_activity WHERE "
                                           "datname=current_database() AND state='idle in transaction' "
                                           "AND query LIKE 'INSERT INTO integration_inbox%' LIMIT 1"))
            assert pid is not None
            try:
                wait_for_waiter(Session, pid)
            finally:
                release.set()
            assert admission.result(timeout=5).code == "ACCEPTED"
            revocation.result(timeout=5)
    finally:
        release.set()
        event.remove(engine, "after_cursor_execute", hold)
    assert count(Session) == 1
    assert send(Session, world).code == "UNAUTHENTICATED"


def test_commit_failure_returns_no_receipt_and_rolls_back(Session, world):
    def fail(session):
        session.flush()
        raise OperationalError("synthetic commit failure", None, None)
    event.listen(Session, "before_commit", fail)
    try:
        result = send(Session, world)
        assert result.code == "INGRESS_UNAVAILABLE" and result.receipt_id is None
    finally:
        event.remove(Session, "before_commit", fail)
    assert count(Session) == 0
    assert send(Session, world).code == "ACCEPTED"


@pytest.mark.parametrize("sql", [
    "UPDATE integration_bindings SET tenant_id = 'other'",
    "DELETE FROM integration_credentials",
    "UPDATE integration_credentials SET binding_id = 'other'",
    "DELETE FROM integration_inbox",
    "UPDATE integration_inbox SET raw_body = 'changed'",
])
def test_database_rejects_identity_reset(Session, world, sql):
    assert send(Session, world).code == "ACCEPTED"
    with pytest.raises(DBAPIError), Session.begin() as session:
        session.execute(text(sql))
    assert count(Session) == 1


def test_accountless_namespace_cannot_be_reenrolled(Session, world):
    with pytest.raises(IntegrityError):
        provision_binding(Session, replace(world[0], binding_id="replacement"))
    disable_binding(Session, world[0].binding_id)
    assert send(Session, world).code == "BINDING_FORBIDDEN"


def test_lock_timeout_has_no_ack(Session, world):
    with Session.begin() as blocker, ThreadPoolExecutor(1) as pool:
        blocker.execute(select(TenantRow).where(TenantRow.id == A).with_for_update())
        result = pool.submit(send, Session, world).result(timeout=5)
    assert result.code == "INGRESS_UNAVAILABLE" and result.receipt_id is None
    assert count(Session) == 0


def test_expiry_is_checked_after_lock_wait(Session, world):
    token = secrets.token_urlsafe(32)
    expiry = datetime.now(timezone.utc) + timedelta(seconds=0.8)
    provision_credential(Session, replace(world[1], credential_id="short-lived",
                                          digest=credential_digest(token), expires_at=expiry))
    with ThreadPoolExecutor(1) as pool:
        with Session.begin() as blocker:
            blocker.execute(select(TenantRow).where(TenantRow.id == A).with_for_update())
            pid = blocker.scalar(text("SELECT pg_backend_pid()"))
            future = pool.submit(send, Session, world, token=token)
            wait_for_waiter(Session, pid)
            while blocker.scalar(text("SELECT clock_timestamp()")) < expiry:
                time.sleep(0.01)
        assert future.result(timeout=5).code == "UNAUTHENTICATED"
    assert count(Session) == 0


def test_separate_tenants_have_separate_receipts(Session, world):
    binding = replace(world[0], binding_id="binding-b", tenant_id=B)
    token = secrets.token_urlsafe(32)
    provision_binding(Session, binding)
    provision_credential(Session, replace(world[1], credential_id="credential-b",
                                          binding_id=binding.binding_id,
                                          digest=credential_digest(token)))
    payload = {**world[3], "tenant_id": B}
    first = send(Session, world)
    second = send(Session, world, payload, token)
    assert first.code == second.code == "ACCEPTED"
    assert first.receipt_id != second.receipt_id and count(Session) == 2


def test_revoked_credentials_cannot_be_resurrected(Session, world):
    revoke_credential(Session, world[1].credential_id)
    with pytest.raises(DBAPIError), Session.begin() as session:
        session.get(CredentialRow, world[1].credential_id).revoked = False
    assert send(Session, world).code == "UNAUTHENTICATED"


def test_composite_foreign_key_rejects_cross_tenant_inbox(Session, world):
    with pytest.raises(IntegrityError), Session.begin() as session:
        session.add(InboxRow(id="forged", tenant_id=B, binding_id=world[0].binding_id,
                             credential_id=world[1].credential_id, contract_type="inbound_event",
                             external_event_id="event-1", idempotency_key="event-1",
                             body_sha256="0" * 64, raw_body=b'{}', state="PENDING",
                             admitted_at=datetime.now(timezone.utc), correlation_id="corr-1"))
    assert count(Session) == 0


def test_populated_downgrade_refuses_to_drop_receipts(Session, world, pg_url):
    import os
    import subprocess
    import sys

    assert send(Session, world).code == "ACCEPTED"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0036_artifact_registry_v0"],
        env={**os.environ, "DATABASE_URL": pg_url}, capture_output=True, text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "INTEGRATION_ADMISSION_DOWNGRADE_REQUIRES_DATA_EXPORT" in result.stderr
    assert count(Session) == 1
    with Session() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0037_integration_admission_v0"
        )


@pytest.mark.parametrize("model,identity", [(BindingRow, "binding-a"),
                                            (CredentialRow, "credential-a")])
@pytest.mark.parametrize("scopes", [{INBOUND_SCOPE: True}, INBOUND_SCOPE])
def test_malformed_stored_scopes_fail_closed(Session, world, model, identity, scopes):
    with Session.begin() as session:
        session.get(model, identity).scopes = scopes
    assert send(Session, world).code == "INGRESS_UNAVAILABLE"
    assert count(Session) == 0
