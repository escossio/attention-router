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
    MAESTRO_RELAY_PATH, get_maestro_relay_session_factory,
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
    app = create_ingress_app()
    app.dependency_overrides[get_maestro_relay_session_factory] = lambda: Session
    with TestClient(app) as client:
        data = envelope()
        headers = {"Authorization": f"Bearer {token}",
                   "Idempotency-Key": "zabbix:1"}
        disabled = client.post(MAESTRO_RELAY_PATH, json=data, headers=headers)
        assert disabled.status_code == 503
        assert receipts(Session) == 0
        monkeypatch.setattr(settings, "maestro_relay_ingress_enabled", True)
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
