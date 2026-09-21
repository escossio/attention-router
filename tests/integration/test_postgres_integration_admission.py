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
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError, DBAPIError

from attention_router.infrastructure.models import (
    ActorBindingRow,
    CanonicalEventRow,
    IntegrationBindingRow as BindingRow,
    IntegrationCredentialRow as CredentialRow,
    IntegrationInboxRow as InboxRow,
    TenantRow,
    TimelineEventRow,
)
from attention_router.integrations.admission import (
    admit_inbound,
    disable_binding,
    provision_binding,
    provision_credential,
    provision_installation,
    revoke_credential,
)
from attention_router.integrations.tenant_binding import (
    INBOUND_SCOPE, IntegrationBinding, CredentialRecord, credential_digest,
)
from attention_router.integrations.dispatch import (
    integration_actor_binding_source,
    process_integration_inbox,
)
from attention_router.domain.models import new_id
from attention_router.config import settings
from attention_router.web.ingress_app import create_ingress_app
from attention_router.web.integration_ingress import (
    INTEGRATION_INGRESS_PATH,
    get_integration_session_factory,
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


def test_atomic_first_install_creates_binding_and_credential(Session, world):
    now = datetime.now(timezone.utc)
    binding_id = "binding-" + new_id()
    credential_id = "credential-" + new_id()
    instance_id = "mailbox-" + new_id()[:20]
    binding = IntegrationBinding(
        binding_id,
        "test-ingress",
        A,
        "CHANNEL",
        "channel.email",
        instance_id,
        "owner@example.invalid",
        True,
        frozenset({INBOUND_SCOPE}),
    )
    token = secrets.token_urlsafe(32)
    credential = CredentialRecord(
        credential_id,
        credential_digest(token),
        binding_id,
        now - timedelta(minutes=1),
        now + timedelta(hours=24),
        False,
        binding.scopes,
    )

    provision_installation(Session, binding, credential)

    with Session() as session:
        stored_binding = session.get(BindingRow, binding_id)
        stored_credential = session.get(CredentialRow, credential_id)
        assert stored_binding is not None
        assert stored_binding.name == "channel.email"
        assert stored_binding.instance_id == instance_id
        assert stored_credential is not None
        assert stored_credential.binding_id == binding_id
        assert stored_credential.digest == credential_digest(token)


def test_atomic_first_install_rejects_mismatched_credential_without_partial_state(
    Session,
    world,
):
    now = datetime.now(timezone.utc)
    binding_id = "binding-" + new_id()
    binding = IntegrationBinding(
        binding_id,
        "test-ingress",
        A,
        "CHANNEL",
        "channel.email",
        "mailbox-" + new_id()[:20],
        None,
        True,
        frozenset({INBOUND_SCOPE}),
    )
    credential = CredentialRecord(
        "credential-" + new_id(),
        credential_digest(secrets.token_urlsafe(32)),
        "different-binding",
        now - timedelta(minutes=1),
        now + timedelta(hours=1),
        False,
        binding.scopes,
    )

    with pytest.raises(ValueError, match="Credential binding mismatch"):
        provision_installation(Session, binding, credential)

    with Session() as session:
        assert session.get(BindingRow, binding_id) is None
        assert session.get(
            CredentialRow,
            credential.credential_id,
        ) is None


def test_atomic_first_install_namespace_conflict_rolls_back_credential(
    Session,
    world,
):
    now = datetime.now(timezone.utc)
    binding_id = "binding-" + new_id()
    credential_id = "credential-" + new_id()
    conflicting = IntegrationBinding(
        binding_id,
        world[0].audience,
        world[0].tenant_id,
        world[0].kind,
        world[0].name,
        world[0].instance_id,
        world[0].account_id,
        True,
        world[0].scopes,
    )
    credential = CredentialRecord(
        credential_id,
        credential_digest(secrets.token_urlsafe(32)),
        binding_id,
        now - timedelta(minutes=1),
        now + timedelta(hours=1),
        False,
        conflicting.scopes,
    )

    with pytest.raises(
        ValueError,
        match="Integration namespace already exists",
    ):
        provision_installation(Session, conflicting, credential)

    with Session() as session:
        assert session.get(BindingRow, binding_id) is None
        assert session.get(CredentialRow, credential_id) is None


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
            "0045_provider_authorization_v1"
        )


@pytest.mark.parametrize("model,identity", [(BindingRow, "binding-a"),
                                            (CredentialRow, "credential-a")])
@pytest.mark.parametrize("scopes", [{INBOUND_SCOPE: True}, INBOUND_SCOPE])
def test_malformed_stored_scopes_fail_closed(Session, world, model, identity, scopes):
    with Session.begin() as session:
        session.get(model, identity).scopes = scopes
    assert send(Session, world).code == "INGRESS_UNAVAILABLE"
    assert count(Session) == 0



@pytest.fixture
def integration_http_client(Session, monkeypatch):
    monkeypatch.setattr(settings, "integration_ingress_enabled", True)
    monkeypatch.setattr(settings, "integration_ingress_audience", "test-ingress")
    app = create_ingress_app()
    app.dependency_overrides[get_integration_session_factory] = lambda: Session
    with TestClient(app) as client:
        yield client


def _http_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }


def _http_raw(world, payload=None):
    return json.dumps(
        payload or world[3],
        separators=(",", ":"),
    ).encode()


def test_neutral_http_receiver_commits_before_202_and_replays_as_200(
    Session,
    world,
    integration_http_client,
):
    raw = _http_raw(world)
    first = integration_http_client.post(
        INTEGRATION_INGRESS_PATH,
        content=raw,
        headers=_http_headers(world[2]),
    )

    assert first.status_code == 202
    assert first.headers["cache-control"] == "no-store"
    assert set(first.json()) == {
        "transport_version",
        "status",
        "receipt_id",
        "admitted_at",
        "correlation_id",
    }
    assert first.json()["transport_version"] == "1"
    assert first.json()["status"] == "accepted"
    assert first.json()["correlation_id"] == world[3]["correlation_id"]

    with Session() as session:
        row = session.get(InboxRow, first.json()["receipt_id"])
        assert row is not None
        assert row.state == "PENDING"
        assert row.raw_body == raw
        assert row.tenant_id == A
        assert row.binding_id == world[0].binding_id

    duplicate = integration_http_client.post(
        INTEGRATION_INGRESS_PATH,
        content=raw,
        headers=_http_headers(world[2]),
    )
    assert duplicate.status_code == 200
    assert duplicate.headers["cache-control"] == "no-store"
    assert duplicate.json()["status"] == "duplicate"
    assert duplicate.json()["receipt_id"] == first.json()["receipt_id"]
    assert duplicate.json()["admitted_at"] == first.json()["admitted_at"]
    assert count(Session) == 1


def test_neutral_http_receiver_preserves_binding_and_idempotency_fail_closed(
    Session,
    world,
    integration_http_client,
):
    wrong_tenant = copy.deepcopy(world[3])
    wrong_tenant["tenant_id"] = B
    denied = integration_http_client.post(
        INTEGRATION_INGRESS_PATH,
        content=_http_raw(world, wrong_tenant),
        headers=_http_headers(world[2]),
    )
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "BINDING_FORBIDDEN"
    assert set(denied.json()) == {
        "transport_version",
        "error_code",
        "request_id",
    }
    assert count(Session) == 0

    raw = _http_raw(world)
    accepted = integration_http_client.post(
        INTEGRATION_INGRESS_PATH,
        content=raw,
        headers=_http_headers(world[2]),
    )
    assert accepted.status_code == 202

    changed_bytes = json.dumps(world[3], indent=2).encode()
    conflict = integration_http_client.post(
        INTEGRATION_INGRESS_PATH,
        content=changed_bytes,
        headers=_http_headers(world[2]),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert count(Session) == 1


@pytest.mark.parametrize(
    "authorization",
    [
        "Basic synthetic",
        "Bearer short",
        "Bearer " + ("A" * 42 + "=A"),
        "Bearer " + ("A" * 513),
    ],
)
def test_neutral_http_receiver_rejects_malformed_bearer_representation(
    Session,
    world,
    integration_http_client,
    authorization,
):
    response = integration_http_client.post(
        INTEGRATION_INGRESS_PATH,
        content=_http_raw(world),
        headers={
            "Authorization": authorization,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 401
    assert response.json()["error_code"] == "UNAUTHENTICATED"
    assert response.headers["www-authenticate"] == (
        'Bearer realm="andy-integration-ingress", error="invalid_token"'
    )
    assert count(Session) == 0


def test_neutral_http_receiver_auth_precedes_private_payload_diagnostics(
    Session,
    world,
    integration_http_client,
):
    invalid_token = secrets.token_urlsafe(32)
    response = integration_http_client.post(
        INTEGRATION_INGRESS_PATH,
        content=b'{"broken"',
        headers=_http_headers(invalid_token),
    )

    assert response.status_code == 401
    assert response.json()["error_code"] == "UNAUTHENTICATED"
    assert response.headers["www-authenticate"] == (
        'Bearer realm="andy-integration-ingress", error="invalid_token"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert count(Session) == 0

    missing = integration_http_client.post(
        INTEGRATION_INGRESS_PATH,
        content=b'{"broken"',
        headers={"Content-Type": "application/json"},
    )
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == (
        'Bearer realm="andy-integration-ingress"'
    )
    assert missing.json()["error_code"] == "UNAUTHENTICATED"


@pytest.mark.parametrize(
    "headers,path,status_code,error_code",
    [
        (
            {"Idempotency-Key": "alternate"},
            INTEGRATION_INGRESS_PATH,
            400,
            "INVALID_REQUEST",
        ),
        (
            {"X-Tenant-ID": A},
            INTEGRATION_INGRESS_PATH,
            400,
            "INVALID_REQUEST",
        ),
        (
            {},
            INTEGRATION_INGRESS_PATH + "?tenant=" + A,
            400,
            "INVALID_REQUEST",
        ),
        (
            {"Content-Encoding": "gzip"},
            INTEGRATION_INGRESS_PATH,
            415,
            "UNSUPPORTED_MEDIA_TYPE",
        ),
    ],
)
def test_neutral_http_receiver_rejects_alternate_authority_and_encoding(
    Session,
    world,
    integration_http_client,
    headers,
    path,
    status_code,
    error_code,
):
    merged = {**_http_headers(world[2]), **headers}
    response = integration_http_client.post(
        path,
        content=_http_raw(world),
        headers=merged,
    )
    assert response.status_code == status_code
    assert response.json()["error_code"] == error_code
    assert response.headers["cache-control"] == "no-store"
    assert count(Session) == 0


def test_neutral_http_receiver_rejects_mixed_and_duplicate_auth(
    Session,
    world,
    integration_http_client,
):
    mixed = integration_http_client.post(
        INTEGRATION_INGRESS_PATH,
        content=_http_raw(world),
        headers={
            **_http_headers(world[2]),
            "X-Attention-Signature": "legacy",
        },
    )
    assert mixed.status_code == 400
    assert mixed.json()["error_code"] == "INVALID_REQUEST"

    duplicate = integration_http_client.post(
        INTEGRATION_INGRESS_PATH,
        content=_http_raw(world),
        headers=[
            ("Authorization", f"Bearer {world[2]}"),
            ("Authorization", f"Bearer {world[2]}"),
            ("Content-Type", "application/json"),
        ],
    )
    assert duplicate.status_code == 400
    assert duplicate.json()["error_code"] == "INVALID_REQUEST"
    assert count(Session) == 0


def test_neutral_http_receiver_enforces_media_body_and_contract_profiles(
    Session,
    world,
    integration_http_client,
):
    media = integration_http_client.post(
        INTEGRATION_INGRESS_PATH,
        content=_http_raw(world),
        headers={
            "Authorization": f"Bearer {world[2]}",
            "Content-Type": "text/plain",
        },
    )
    assert media.status_code == 415
    assert media.json()["error_code"] == "UNSUPPORTED_MEDIA_TYPE"

    oversized = integration_http_client.post(
        INTEGRATION_INGRESS_PATH,
        content=b" " * 65_537,
        headers=_http_headers(world[2]),
    )
    assert oversized.status_code == 413
    assert oversized.json()["error_code"] == "BODY_TOO_LARGE"

    invalid_contract = integration_http_client.post(
        INTEGRATION_INGRESS_PATH,
        content=b"{}",
        headers=_http_headers(world[2]),
    )
    assert invalid_contract.status_code == 422
    assert invalid_contract.json()["error_code"] == "INVALID_CONTRACT"
    assert count(Session) == 0


def test_neutral_http_receiver_method_and_default_disabled_behavior(
    Session,
    world,
    monkeypatch,
    integration_http_client,
):
    method = integration_http_client.get(INTEGRATION_INGRESS_PATH)
    assert method.status_code == 405
    assert method.headers["allow"] == "POST"
    assert method.headers["cache-control"] == "no-store"
    assert method.json()["error_code"] == "METHOD_NOT_ALLOWED"

    monkeypatch.setattr(settings, "integration_ingress_enabled", False)
    disabled = integration_http_client.post(
        INTEGRATION_INGRESS_PATH,
        content=_http_raw(world),
        headers=_http_headers(world[2]),
    )
    assert disabled.status_code == 503
    assert disabled.json()["error_code"] == "INGRESS_UNAVAILABLE"
    assert count(Session) == 0



def _payload_with_actor(world, *, external_actor_id="sender@example.invalid"):
    payload = copy.deepcopy(world[3])
    payload["actor"] = {
        "external_actor_id": external_actor_id,
        "display_name": "Synthetic Sender",
    }
    payload["thread"] = {
        "external_thread_id": "thread-1",
        "kind": "THREAD",
        "title": None,
    }
    payload["payload_type"] = "EMAIL_MESSAGE_REFERENCE"
    payload["payload_ref"] = {
        "message_ref": "provider-private-message-ref"
    }
    return payload


def _install_integration_actor_binding(
    Session,
    world,
    *,
    source_binding_id=None,
    external_actor_id="sender@example.invalid",
    actor_key="contact-email",
):
    stamp = datetime.now(timezone.utc)
    with Session.begin() as session:
        session.add(
            ActorBindingRow(
                id=new_id(),
                tenant_id=A,
                source=integration_actor_binding_source(
                    source_binding_id or world[0].binding_id
                ),
                external_actor_id=external_actor_id,
                actor_key=actor_key,
                display_name="Synthetic Sender",
                actor_category="contact",
                active_context=None,
                is_active=True,
                binding_metadata={
                    "integration_binding_id": (
                        source_binding_id or world[0].binding_id
                    )
                },
                created_at=stamp,
                updated_at=stamp,
            )
        )


def test_dispatch_bridges_email_contract_losslessly_by_durable_reference(
    Session,
    world,
):
    payload = _payload_with_actor(world)
    admitted = send(Session, world, payload)
    assert admitted.code == "ACCEPTED"
    _install_integration_actor_binding(Session, world)

    with Session.begin() as session:
        result = process_integration_inbox(session, limit=10)

    assert result.selected == 1
    assert result.processed == 1
    assert result.blocked == 0

    with Session() as session:
        inbox = session.get(InboxRow, admitted.receipt_id)
        assert inbox.state == "PROCESSED"
        assert inbox.processed_at is not None
        assert inbox.dispatch_reason is None
        assert inbox.canonical_event_id is not None

        canonical = session.get(CanonicalEventRow, inbox.canonical_event_id)
        assert canonical is not None
        assert canonical.tenant_id == A
        assert canonical.origin == "EXTERNAL_INBOUND"
        assert canonical.event_type == "message"
        assert canonical.actor_id == "contact-email"
        assert canonical.channel == "channel.email"
        assert canonical.payload_type == "EMAIL_MESSAGE_REFERENCE"
        assert canonical.payload_ref == {
            "integration_inbox_id": inbox.id
        }
        assert canonical.correlation_id == payload["correlation_id"]
        assert canonical.occurred_at == datetime.fromisoformat(
            payload["occurred_at"]
        )
        assert canonical.received_at == datetime.fromisoformat(
            payload["received_at"]
        )
        assert canonical.metadata_sanitized["integration_binding_id"] == (
            world[0].binding_id
        )
        assert canonical.metadata_sanitized["actor_resolved"] is True
        assert canonical.metadata_sanitized["thread_present"] is True

        # Provider-private identifiers remain recoverable only through the
        # authenticated durable inbox reference, not copied into canonical data.
        rendered = json.dumps(
            {
                "payload_ref": canonical.payload_ref,
                "metadata": canonical.metadata_sanitized,
            },
            sort_keys=True,
        )
        assert "sender@example.invalid" not in rendered
        assert "provider-private-message-ref" not in rendered
        assert "thread-1" not in rendered

        timeline = session.scalar(
            select(TimelineEventRow).where(
                TimelineEventRow.canonical_event_id == canonical.id
            )
        )
        assert timeline is not None
        assert timeline.actor_id == "contact-email"
        assert timeline.event_type == "MESSAGE_RECEIVED"
        assert timeline.provenance == "integration_dispatch"
        assert timeline.event_ref["integration_inbox_id"] == inbox.id

    with Session.begin() as session:
        replay = process_integration_inbox(session, limit=10)
    assert replay.selected == 0
    with Session() as session:
        assert session.scalar(
            select(func.count()).select_from(CanonicalEventRow)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(TimelineEventRow)
        ) == 1


def test_dispatch_never_cross_resolves_actor_from_another_integration_namespace(
    Session,
    world,
):
    payload = _payload_with_actor(world)
    admitted = send(Session, world, payload)
    assert admitted.code == "ACCEPTED"
    _install_integration_actor_binding(
        Session,
        world,
        source_binding_id="different-binding",
        actor_key="wrong-contact",
    )

    with Session.begin() as session:
        result = process_integration_inbox(session)

    assert result.processed == 1
    with Session() as session:
        inbox = session.get(InboxRow, admitted.receipt_id)
        canonical = session.get(CanonicalEventRow, inbox.canonical_event_id)
        assert canonical.actor_id is None
        assert canonical.metadata_sanitized["external_actor_present"] is True
        assert canonical.metadata_sanitized["actor_resolved"] is False


def test_dispatch_blocks_work_if_binding_is_disabled_after_admission(
    Session,
    world,
):
    admitted = send(Session, world, _payload_with_actor(world))
    assert admitted.code == "ACCEPTED"
    disable_binding(Session, world[0].binding_id)

    with Session.begin() as session:
        result = process_integration_inbox(session)

    assert result.selected == 1
    assert result.processed == 0
    assert result.blocked == 1
    with Session() as session:
        inbox = session.get(InboxRow, admitted.receipt_id)
        assert inbox.state == "BLOCKED"
        assert inbox.canonical_event_id is None
        assert inbox.processed_at is not None
        assert inbox.dispatch_reason == "INTEGRATION_BINDING_INACTIVE"
        assert session.scalar(
            select(func.count()).select_from(CanonicalEventRow)
        ) == 0


def test_dispatch_blocks_work_if_binding_scope_is_removed_after_admission(
    Session,
    world,
):
    admitted = send(Session, world, _payload_with_actor(world))
    assert admitted.code == "ACCEPTED"
    with Session.begin() as session:
        session.get(BindingRow, world[0].binding_id).scopes = []

    with Session.begin() as session:
        result = process_integration_inbox(session)

    assert result.blocked == 1
    with Session() as session:
        inbox = session.get(InboxRow, admitted.receipt_id)
        assert inbox.state == "BLOCKED"
        assert inbox.dispatch_reason == "INTEGRATION_BINDING_SCOPE_INACTIVE"
        assert session.scalar(
            select(func.count()).select_from(CanonicalEventRow)
        ) == 0


def test_dispatch_serializes_against_binding_disable_that_commits_first(
    Session,
    world,
):
    admitted = send(Session, world, _payload_with_actor(world))
    assert admitted.code == "ACCEPTED"

    def run_dispatch():
        with Session.begin() as session:
            return process_integration_inbox(session)

    with ThreadPoolExecutor(1) as pool:
        with Session.begin() as blocker:
            blocker.execute(
                select(TenantRow)
                .where(TenantRow.id == A)
                .with_for_update()
            )
            binding = blocker.scalar(
                select(BindingRow)
                .where(BindingRow.id == world[0].binding_id)
                .with_for_update()
            )
            binding.active = False
            pid = blocker.scalar(text("SELECT pg_backend_pid()"))
            future = pool.submit(run_dispatch)
            wait_for_waiter(Session, pid)

        result = future.result(timeout=5)

    assert result.processed == 0
    assert result.blocked == 1
    with Session() as session:
        inbox = session.get(InboxRow, admitted.receipt_id)
        assert inbox.state == "BLOCKED"
        assert inbox.dispatch_reason == "INTEGRATION_BINDING_INACTIVE"
        assert session.scalar(
            select(func.count()).select_from(CanonicalEventRow)
        ) == 0


def test_dispatch_blocks_work_if_tenant_is_disabled_after_admission(
    Session,
    world,
):
    admitted = send(Session, world, _payload_with_actor(world))
    assert admitted.code == "ACCEPTED"
    with Session.begin() as session:
        session.get(TenantRow, A).status = "INACTIVE"

    with Session.begin() as session:
        result = process_integration_inbox(session)

    assert result.blocked == 1
    with Session() as session:
        inbox = session.get(InboxRow, admitted.receipt_id)
        assert inbox.state == "BLOCKED"
        assert inbox.dispatch_reason == "INTEGRATION_TENANT_INACTIVE"
        assert session.scalar(
            select(func.count()).select_from(CanonicalEventRow)
        ) == 0


def test_database_rejects_invalid_or_destructive_dispatch_mutations(
    Session,
    world,
):
    admitted = send(Session, world)
    assert admitted.code == "ACCEPTED"

    with pytest.raises(DBAPIError), Session.begin() as session:
        row = session.get(InboxRow, admitted.receipt_id)
        row.state = "PROCESSED"
        row.processed_at = datetime.now(timezone.utc)

    with pytest.raises(DBAPIError), Session.begin() as session:
        row = session.get(InboxRow, admitted.receipt_id)
        row.raw_body = b"changed"

    with Session() as session:
        row = session.get(InboxRow, admitted.receipt_id)
        assert row.state == "PENDING"
        assert row.raw_body != b"changed"


def test_processed_dispatch_state_blocks_downgrade_without_export(
    Session,
    world,
    pg_url,
):
    import os
    import subprocess
    import sys

    admitted = send(Session, world, _payload_with_actor(world))
    assert admitted.code == "ACCEPTED"
    with Session.begin() as session:
        result = process_integration_inbox(session)
        assert result.processed == 1

    downgrade = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "downgrade",
            "0043_pending_intents_v1",
        ],
        env={**os.environ, "DATABASE_URL": pg_url},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert downgrade.returncode != 0
    assert (
        "INTEGRATION_DISPATCH_DOWNGRADE_REQUIRES_DATA_EXPORT"
        in downgrade.stderr
    )
    # The downgrade runs in one PostgreSQL transaction. When the 0044
    # export guard aborts, the attempted 0045 downgrade rolls back as well.
    with Session() as session:
        assert session.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "0045_provider_authorization_v1"
