"""Discardable CI only: simulate provider assertion, never key possession or session authority."""

import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from test_incremental_memory_m1 import lab as m1_lab, b64

from attention_router.core.human_identity import VerifiedProviderIdentity
from attention_router.integrations.google_identity import GoogleIdentityVerifier
from experiments.incremental_memory.authenticated.schema import register_scope
from experiments.incremental_memory.device.composition import (
    create_device_app,
    provision_onboarding,
)

lab = m1_lab
pytestmark = pytest.mark.postgres


@pytest.fixture
def device_lab(lab, monkeypatch):
    engine = lab.admin.kw["bind"]
    provision_onboarding(
        engine,
        issuer_role=lab.issuer_factory.kw["bind"].url.username,
        reader_role=lab.reader_factory.kw["bind"].url.username,
    )
    assertion = {"subject": "synthetic-device-owner", "nonce": ""}

    def verify(self, token):
        now = datetime.now(UTC)
        return VerifiedProviderIdentity(
            "google",
            assertion["subject"],
            assertion["nonce"],
            now - timedelta(seconds=1),
            now + timedelta(minutes=5),
        )

    monkeypatch.setattr(GoogleIdentityVerifier, "verify", verify)
    settings = SimpleNamespace(
        **vars(lab.settings),
        human_identity_enabled=True,
        human_auth_challenge_ttl_seconds=60,
        human_auth_continuation_grant_ttl_seconds=120,
        device_bootstrap_enabled=True,
        device_bootstrap_challenge_ttl_seconds=60,
    )
    app = create_device_app(
        issuer_factory=lab.issuer_factory,
        reader_factory=lab.reader_factory,
        settings=settings,
        google_audience="synthetic-lab.apps.googleusercontent.com",
        allowed_subjects={"synthetic-device-owner"},
    )
    with TestClient(app) as client:
        yield lab, client, assertion


def continuation(client, assertion):
    c = client.post("/api/v1/auth/google/challenges")
    assert c.status_code == 201
    challenge = c.json()
    assertion["nonce"] = challenge["nonce"]
    url = "/api/v1/auth/google/challenges/" + challenge["challenge_id"] + "/verify-and-continue"
    r = client.post(url, json={"id_token": "synthetic-provider-assertion"})
    assert r.status_code == 200
    assert client.post(url, json={"id_token": "synthetic-provider-assertion"}).status_code == 409
    return r.json()["continuation_grant"]["token"]


def test_bootstrap_session_context(device_lab):
    lab, client, assertion = device_lab
    grant = continuation(client, assertion)
    private = ec.generate_private_key(ec.SECP256R1())
    spki = b64(
        private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    payload = {
        "continuation_token": grant,
        "public_key_spki_b64url": spki,
        "canonical_device_name": "Synthetic Lab Phone",
        "platform": "ANDROID",
        "roles": ["CLIENT"],
    }
    route = "/api/v1/bootstrap/device/challenges"
    c = client.post(route, json=payload)
    assert c.status_code == 201
    challenge = c.json()
    raw = base64.urlsafe_b64decode(challenge["challenge_b64url"] + "==")
    wrong = ec.generate_private_key(ec.SECP256R1()).sign(raw, ec.ECDSA(hashes.SHA256()))
    complete = route + "/" + challenge["bootstrap_challenge_id"] + "/complete"
    assert client.post(complete, json={"device_signature_b64url": b64(wrong)}).status_code == 401
    signed = {"device_signature_b64url": b64(private.sign(raw, ec.ECDSA(hashes.SHA256())))}
    enrolled = client.post(complete, json=signed)
    assert enrolled.status_code == 200
    assert client.post(complete, json=signed).status_code == 409
    assert client.post(route, json=payload).status_code == 401
    identity = enrolled.json()
    tenant = identity["initial_tenant_id"]
    assert identity["memberships"][0]["role"] == "OWNER"
    with lab.admin.begin() as s:
        scope = register_scope(
            s,
            owner_id=identity["human_identity_id"],
            tenant_id=tenant,
            information_ids=[lab.recorded["information_id"]],
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    route = "/api/v1/session/device/challenges"
    c = client.post(route, json={"public_key_spki_b64url": spki, "requested_tenant_id": tenant})
    assert c.status_code == 201
    c = c.json()
    raw = base64.urlsafe_b64decode(c["challenge_b64url"] + "==")
    issued = client.post(
        route + "/" + c["session_challenge_id"] + "/complete",
        json={"device_signature_b64url": b64(private.sign(raw, ec.ECDSA(hashes.SHA256())))},
    )
    assert issued.status_code == 200
    headers = {"Authorization": "Bearer " + issued.json()["session"]["session_token"]}
    query = {"scope_ref": scope, "question": "Qual é a cor do carro de Roberto?"}
    result = client.post("/api/v1/client/context/query", headers=headers, json=query)
    assert result.status_code == 200
    assert result.json()["answer"] == "O carro de Roberto é azul."
    assert result.json()["evidence"][0]["evidence_id"] == lab.recorded["evidence_id"]
    query["scope_ref"] = lab.scope
    assert (
        client.post("/api/v1/client/context/query", headers=headers, json=query).status_code == 403
    )


@pytest.mark.parametrize("failure", ["identity", "nonce"])
def test_rejected_provider_identity_or_nonce(device_lab, failure):
    lab, client, assertion = device_lab
    c = client.post("/api/v1/auth/google/challenges").json()
    assertion["nonce"] = c["nonce"] if failure == "identity" else "wrong-nonce"
    if failure == "identity":
        assertion["subject"] = "not-allowed"
    r = client.post(
        "/api/v1/auth/google/challenges/" + c["challenge_id"] + "/verify-and-continue",
        json={"id_token": "synthetic-provider-assertion"},
    )
    assert r.status_code == 401
    with lab.admin.begin() as s:
        assert (
            s.execute(text("SELECT count(*) FROM human_auth_continuation_grants")).scalar_one() == 0
        )
