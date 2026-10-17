"""Disposable PostgreSQL only, never a real tenant or live ingress connection."""
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from attention_router.config import settings
from attention_router.infrastructure.models import MaestroRelayReceiptRow, TenantRow
from attention_router.integrations.admission import (
    provision_installation, revoke_credential,
)
from attention_router.integrations.tenant_binding import (
    CredentialRecord, IntegrationBinding, credential_digest,
)
from attention_router.maestro.admission import RELAY_AUDIENCE, RELAY_WRITE_SCOPE, admit_maestro_relay
from attention_router.web.ingress_app import create_ingress_app
from attention_router.web.maestro_relay_ingress import (
    MAESTRO_RELAY_PATH, create_maestro_tls_app, get_maestro_relay_session_factory,
)

pytestmark = pytest.mark.postgres
TENANT_A = "00000000-0000-4000-8000-000000009101"
TENANT_B = "00000000-0000-4000-8000-000000009102"


@pytest.fixture
def relay_world(Session):
    now = datetime.now(timezone.utc)
    with Session.begin() as session:
        for tenant_id in (TENANT_A, TENANT_B):
            session.add(TenantRow(
                id=tenant_id, slug=tenant_id, name="Synthetic relay",
                status="ACTIVE", created_at=now, updated_at=now,
            ))
    token = secrets.token_urlsafe(32)
    scope = frozenset({RELAY_WRITE_SCOPE})
    binding = IntegrationBinding(
        "maestro-relay-binding-1", RELAY_AUDIENCE, TENANT_A, "CAPABILITY",
        "ops.zabbix_relay", "relay-stage-1", None, True, scope,
    )
    credential = CredentialRecord(
        "maestro-relay-credential-1", credential_digest(token), binding.binding_id,
        now - timedelta(hours=1), now + timedelta(hours=1), False, scope,
    )
    provision_installation(Session, binding, credential)
    return Session, token, binding, credential


def envelope(tenant=TENANT_A, number=1, status="PROBLEM"):
    signal = {
        "schema_version": "1", "event_id": "problem:synthetic:123",
        "occurred_at": "2026-10-17T12:00:00Z", "asset_ref": "synthetic:router",
        "signal_code": "ZBX_TRIGGER:777", "status": status, "severity": 3,
    }
    if status == "RECOVERY":
        signal["event_id"] = "recovery:synthetic:124"
        signal["problem_event_id"] = "problem:synthetic:123"
    return {
        "contract_version": "occurrence-relay-v0-draft",
        "occurrence_number": number, "source_system": "zabbix",
        "tenant_id": tenant, "signal": signal,
    }


def body(value):
    return json.dumps(value, sort_keys=True).encode("utf-8")


def receipts(Session):
    with Session() as session:
        return session.scalar(select(func.count()).select_from(MaestroRelayReceiptRow))


def test_pg_commit_before_ack_and_identical_replay(relay_world):
    Session, token, *_ = relay_world
    raw = body(envelope())
    first = admit_maestro_relay(Session, token, raw, idempotency_key="zabbix:1")
    assert first.code == "ACCEPTED"
    assert first.receipt_id
    assert receipts(Session) == 1
    again = admit_maestro_relay(Session, token, raw, idempotency_key="zabbix:1")
    assert again.code == "DUPLICATE"
    assert again.receipt_id == first.receipt_id
    assert receipts(Session) == 1


def test_pg_conflict_cross_tenant_and_recovery(relay_world):
    Session, token, *_ = relay_world
    first = admit_maestro_relay(Session, token, body(envelope()), idempotency_key="zabbix:1")
    assert first.code == "ACCEPTED"
    conflict = envelope()
    conflict["signal"]["severity"] = 5
    assert admit_maestro_relay(Session, token, body(conflict), idempotency_key="zabbix:1").code == "CONFLICT"
    assert admit_maestro_relay(Session, token, body(envelope(TENANT_B)), idempotency_key="zabbix:1").code == "INVALID_CONTRACT"
    second = admit_maestro_relay(
        Session, token, body(envelope(number=2, status="RECOVERY")), idempotency_key="zabbix:2",
    )
    assert second.code == "ACCEPTED"
    with Session() as db:
        rows = db.scalars(select(MaestroRelayReceiptRow).order_by(MaestroRelayReceiptRow.occurrence_number)).all()
        assert [row.signal_status for row in rows] == ["PROBLEM", "RECOVERY"]
        assert rows[1].problem_event_id == "problem:synthetic:123"
        assert rows[1].signal_json["status"] == "RECOVERY"


def test_pg_revocation_and_invalid_token_never_ack(relay_world):
    Session, token, _binding, credential = relay_world
    assert admit_maestro_relay(Session, "x", body(envelope()), idempotency_key="zabbix:1").code == "UNAUTHENTICATED"
    revoke_credential(Session, credential.credential_id)
    assert admit_maestro_relay(Session, token, body(envelope()), idempotency_key="zabbix:1").code == "UNAUTHENTICATED"
    assert receipts(Session) == 0


def test_pg_tenant_serialization_avoids_duplicate_receipts(relay_world):
    Session, token, *_ = relay_world
    payload = body(envelope())
    def send(_):
        return admit_maestro_relay(Session, token, payload, idempotency_key="zabbix:1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(send, range(2)))
    assert {outcome.code for outcome in outcomes} == {"ACCEPTED", "DUPLICATE"}
    assert outcomes[0].receipt_id == outcomes[1].receipt_id
    assert receipts(Session) == 1


def test_http_route_default_disabled_and_optin_commits(relay_world, monkeypatch):
    Session, token, *_ = relay_world
    app = create_maestro_tls_app()
    app.dependency_overrides[get_maestro_relay_session_factory] = lambda: Session
    with TestClient(app, base_url="https://testserver") as client:
        data = envelope()
        headers = {"Authorization": f"Bearer {token}",
                   "Idempotency-Key": "zabbix:1"}
        disabled = client.post(MAESTRO_RELAY_PATH, json=data, headers=headers)
        assert disabled.status_code == 503
        assert receipts(Session) == 0
        monkeypatch.setattr(settings, "maestro_relay_ingress_enabled", True)
        # The original HTTP ingress must remain completely unaware of MAESTRO.
        with TestClient(create_ingress_app()) as legacy:
            denied = legacy.post(MAESTRO_RELAY_PATH, json=data, headers=headers)
            assert denied.status_code == 404
        assert receipts(Session) == 0
        accepted = client.post(MAESTRO_RELAY_PATH, json=data, headers=headers)
        assert accepted.status_code == 202
        assert accepted.json()["status"] == "ACCEPTED"
        duplicate = client.post(MAESTRO_RELAY_PATH, json=data, headers=headers)
        assert duplicate.status_code == 200
        assert duplicate.json()["receipt_id"] == accepted.json()["receipt_id"]
        assert receipts(Session) == 1
        no_token = client.post(MAESTRO_RELAY_PATH, json=data, headers={"Idempotency-Key": "zabbix:2"})
        assert no_token.status_code == 401
        assert receipts(Session) == 1


def test_pg_populated_maestro_receipts_block_destructive_downgrade(relay_world, pg_url):
    import os
    import subprocess
    import sys
    from pathlib import Path
    from sqlalchemy import text

    Session, token, *_ = relay_world
    received = admit_maestro_relay(
        Session, token, body(envelope()), idempotency_key="zabbix:1",
    )
    assert received.code == "ACCEPTED"
    env = os.environ.copy()
    env["DATABASE_URL"] = pg_url
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0058_group_contextual_presence"],
        cwd=repo, env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "MAESTRO_RELAY_DOWNGRADE_REQUIRES_DATA_EXPORT" in result.stderr
    with Session() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0059_maestro_relay_receipts"
        )
    assert receipts(Session) == 1


def test_loopback_https_relay_shaped_requests_commit_and_replay(relay_world, monkeypatch, tmp_path):
    """Real TLS socket and ingress ASGI app; disposable DB, synthetic credential.

    This is an interoperability gate, not proof that the separate Relay V0
    container is connected to the production MAESTRO endpoint.
    """
    from datetime import timedelta
    from ipaddress import ip_address
    import socket
    import ssl
    import threading
    import time
    from urllib.error import HTTPError, URLError
    from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

    import uvicorn
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    Session, token, *_ = relay_world
    # Short-lived synthetic CA trusted only by this test client.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=30))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ip_address("127.0.0.1")),
            ]), critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / "synthetic-loopback-cert.pem"
    key_file = tmp_path / "synthetic-loopback-key.pem"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)
    tls_context = ssl.create_default_context(cafile=str(cert_file))
    trusted_opener = build_opener(ProxyHandler({}), HTTPSHandler(context=tls_context))
    untrusted_opener = build_opener(
        ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context())
    )
    app = create_maestro_tls_app()
    app.dependency_overrides[get_maestro_relay_session_factory] = lambda: Session
    monkeypatch.setattr(settings, "maestro_relay_ingress_enabled", True)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        ssl_certfile=str(cert_file),
        ssl_keyfile=str(key_file),
        lifespan="off",
        access_log=False,
        log_level="error",
    ))
    # uvicorn receives the already bound socket; no public network attachment.
    task = threading.Thread(
        target=lambda: server.run(sockets=[listener]),
        daemon=True,
        name="synthetic-maestro-loopback-https",
    )
    task.start()
    try:
        for _ in range(100):
            if server.started:
                break
            if not task.is_alive():
                raise AssertionError("synthetic HTTPS server did not start")
            time.sleep(0.03)
        assert server.started, "synthetic HTTPS server startup timeout"
        # The certificate must NOT validate under ambient/default roots.
        with pytest.raises(URLError):
            untrusted_opener.open(
                f"https://127.0.0.1:{port}/health/live", timeout=3,
            )

        def post(data, number, *, bearer=token):
            request = Request(
                f"https://127.0.0.1:{port}{MAESTRO_RELAY_PATH}",
                data=body(data),
                headers={
                    "Authorization": "Bearer " + bearer,
                    "Idempotency-Key": f"zabbix:{number}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with trusted_opener.open(request, timeout=3) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                return error.code, json.loads(error.read())

        first_status, first_body = post(envelope(), 1)
        assert first_status == 202
        assert first_body["status"] == "ACCEPTED"
        assert receipts(Session) == 1  # independently reopened DB after ACK

        # Client discards first ACK and retries: must not cause second receipt.
        duplicate_status, duplicate_body = post(envelope(), 1)
        assert duplicate_status == 200
        assert duplicate_body["receipt_id"] == first_body["receipt_id"]
        assert receipts(Session) == 1

        mutated = envelope()
        mutated["signal"]["severity"] = 5
        assert post(mutated, 1)[0] == 409
        assert post(envelope(TENANT_B), 1)[0] == 422
        assert post(envelope(number=2), 2, bearer="untrusted-token")[0] == 401

        recovered_status, recovered_body = post(envelope(number=2, status="RECOVERY"), 2)
        assert recovered_status == 202
        assert recovered_body["occurrence_number"] == 2
        assert receipts(Session) == 2
        with Session() as session:
            records = session.scalars(
                select(MaestroRelayReceiptRow).order_by(
                    MaestroRelayReceiptRow.occurrence_number
                )
            ).all()
            assert [r.signal_status for r in records] == ["PROBLEM", "RECOVERY"]
            assert records[1].problem_event_id == "problem:synthetic:123"

        # V0 integrated proof: dispatch through the actual Relay HTTP/TLS client,
        # not a synthetic urllib caller, using the same disposable PostgreSQL.
        from tests.test_occurrence_relay_v0 import relay as actual_relay

        relay_db = tmp_path / "actual-relay-outbox.sqlite3"
        store = actual_relay.Store(str(relay_db))
        signals = [
            envelope(number=1)["signal"],
            envelope(number=2, status="RECOVERY")["signal"],
        ]
        third = envelope(number=3)
        third["signal"]["event_id"] = "problem:synthetic:125"
        signals.append(third["signal"])
        for expected_number, signal in enumerate(signals, 1):
            accepted_signal, signal_digest = actual_relay.validate(body(signal))
            local_number, created = store.admit(accepted_signal, signal_digest)
            assert created and local_number == expected_number

        relay_endpoint = f"https://127.0.0.1:{port}{MAESTRO_RELAY_PATH}"
        def actual_client(cafile=""):
            return actual_relay.App(
                store, "unused-inbound-token", TENANT_A, "zabbix",
                relay_endpoint, token, cafile,
            )

        # Missing trust must not send an authenticated bearer or drop outbox data.
        assert actual_client().dispatch_one() is False
        assert store.counts()["pending_delivery"] == 3
        assert receipts(Session) == 2

        client = actual_client(str(cert_file))
        # Those two events were already committed by the independent caller:
        # the real Relay must accept only structured 200 DUPLICATE receipts.
        assert client.dispatch_one() is True
        assert client.dispatch_one() is True
        assert store.counts()["pending_delivery"] == 1
        assert receipts(Session) == 2

        # Force the loss of an ACK *after* the genuine backend has committed;
        # the actual Relay must retain the pending event for the same-key retry.
        actual_verify = actual_relay.valid_delivery_ack
        def synthetic_ack_loss_after_commit(response, occurrence_number):
            assert actual_verify(response, occurrence_number) is True
            raise TimeoutError("synthetic ACK lost after durable receipt")

        with monkeypatch.context() as scoped:
            scoped.setattr(actual_relay, "valid_delivery_ack",
                           synthetic_ack_loss_after_commit)
            assert client.dispatch_one() is False
        assert receipts(Session) == 3
        assert store.counts()["pending_delivery"] == 1

        # Reopen the SQLite outbox and retry with a newly initialized client.
        store = actual_relay.Store(str(relay_db))
        assert actual_client(str(cert_file)).dispatch_one() is True
        assert receipts(Session) == 3  # replay did not insert a fourth row
        assert store.counts()["pending_delivery"] == 0
        with Session() as database:
            final = database.scalars(
                select(MaestroRelayReceiptRow).order_by(
                    MaestroRelayReceiptRow.occurrence_number
                )
            ).all()
            assert [item.occurrence_number for item in final] == [1, 2, 3]
            assert final[2].event_id == "problem:synthetic:125"
    finally:
        server.should_exit = True
        task.join(timeout=6)
        if task.is_alive():
            raise AssertionError("synthetic HTTPS server failed to stop")
        listener.close()


def test_pg_upgrade_from_existing_group_0058_to_maestro_0059_keeps_group_state(
    relay_world, pg_url,
):
    """Disposable DB: preserve preexisting group row through incremental upgrade."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    from uuid import uuid4
    from sqlalchemy import text

    Session, token, *_ = relay_world
    now = datetime.now(timezone.utc)
    group_id = "synthetic-preexisting-group@g.us"
    with Session.begin() as session:
        session.execute(
            text(
                "INSERT INTO group_contextual_presence "
                "(id, tenant_id, group_id, mode, observe_after, updated_at) "
                "VALUES (:id,:tenant,:group_id,:mode,:now,:now)"
            ),
            {
                "id": str(uuid4()),
                "tenant": TENANT_A,
                "group_id": group_id,
                "mode": "OBSERVE",
                "now": now,
            },
        )

    env = os.environ.copy()
    env["DATABASE_URL"] = pg_url
    repo = Path(__file__).resolve().parents[2]

    def migration(*commands):
        return subprocess.run(
            [sys.executable, "-m", "alembic", *commands],
            cwd=repo,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

    downgrade = migration("downgrade", "0058_group_contextual_presence")
    assert downgrade.returncode == 0, downgrade.stderr[-1200:]
    with Session() as session:
        assert session.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "0058_group_contextual_presence"
        assert session.scalar(
            text("SELECT mode FROM group_contextual_presence WHERE group_id=:id"),
            {"id": group_id},
        ) == "OBSERVE"

    upgrade = migration("upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr[-1200:]
    with Session() as session:
        assert session.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "0059_maestro_relay_receipts"
        assert session.scalar(
            text("SELECT mode FROM group_contextual_presence WHERE group_id=:id"),
            {"id": group_id},
        ) == "OBSERVE"
    response = admit_maestro_relay(
        Session, token, body(envelope()), idempotency_key="zabbix:1",
    )
    assert response.code == "ACCEPTED"
    assert receipts(Session) == 1
