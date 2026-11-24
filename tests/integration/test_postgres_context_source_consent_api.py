"""Source-decision HTTP contract with real ClientSession and RLS."""

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from attention_router.api.v1.context_source_consent import mount_context_source_consent_for_test
from attention_router.config import settings
from attention_router.infrastructure.artifact_models import ArtifactReceiptRow
from attention_router.infrastructure.client_bootstrap_models import ClientTenantMembershipRow
from attention_router.infrastructure.context_consent_models import ContextSourceConsentRow
from test_postgres_client_context import context_reader  # noqa: F401
from test_postgres_cognitive_lab import lab  # noqa: F401
from test_postgres_context_preactivation import enroll_role  # noqa: F401
from test_postgres_source_consent import consent_clients  # noqa: F401

pytestmark = pytest.mark.postgres
GRANT = "/api/v1/context/source-consent/grant"
REVOKE = "/api/v1/context/source-consent/revoke"


@pytest.mark.parametrize("lab", ["client-query"], indirect=True)
def test_explicit_decisions_with_real_session(Session, consent_clients, monkeypatch):  # noqa: F811
    args, writer, staged, decision, _ = consent_clients
    source = decision("confirm")
    monkeypatch.setattr(settings, "client_session_enabled", True)
    monkeypatch.setattr(settings, "context_source_consent_api_enabled", True)
    app = FastAPI()
    assert mount_context_source_consent_for_test(app, writer=writer)
    revoke = dict(scope_ref="ctx_" + "1" * 32, message_id=source["message_id"])
    grant = revoke | dict(
        decision_ref="fixture:explicit", expires_at=source["expires_at"].isoformat(),
        retention={layer: deadline.isoformat() for layer, deadline in source["retention"]},
    )

    def ask():
        staged().query_and_deliver(**args, question="domingo", deliver=delivered.append)

    delivered = []
    with pytest.raises(ValueError, match="SCOPE_UNAVAILABLE"):
        ask()
    with TestClient(app, headers={"Authorization": "Bearer " + args["bearer"]}) as client:
        assert client.post(
            GRANT, json=grant, headers={"Authorization": "Bearer cst_" + "x" * 43},
        ).status_code == 401
        for digit in ("2", "3"):
            assert client.post(GRANT, json=grant | {"scope_ref": "ctx_" + digit * 32}).status_code == 403
        assert client.post(GRANT, json=grant | {"scope_ref": "ctx_" + "4" * 32}).status_code == 409
        assert client.post(GRANT, json=grant | {"message_revision": "forged"}).status_code == 422
        assert client.post(GRANT, json=grant | {
            "expires_at": (source["expires_at"] + timedelta(days=31)).isoformat(),
        }).status_code == 409
        assert client.post(GRANT, json=grant | {"retention": grant["retention"] | {
            "embedding": (source["expires_at"] + timedelta(seconds=1)).isoformat(),
        }}).status_code == 409
        withdrawn = decision("proposal")
        with Session.begin() as db:
            receipt = db.get(ArtifactReceiptRow, withdrawn["receipt_id"])
            receipt.metadata_json = {**receipt.metadata_json, "context_evidence_withdrawn": True}
        assert client.post(GRANT, json=grant | {"message_id": withdrawn["message_id"]}).status_code == 409
        with Session() as db:
            assert db.scalars(select(ContextSourceConsentRow)).all() == []
            receipt_revision = db.get(ArtifactReceiptRow, source["receipt_id"]).context_revision

        response = client.post(GRANT, json=grant)
        assert response.status_code == 204 and response.content == b""
        assert response.headers["cache-control"] == "no-store"
        assert client.post(GRANT, json=grant).status_code == 204
        with Session() as db:
            sealed = db.scalars(select(ContextSourceConsentRow)).one()
            assert (sealed.message_revision, sealed.receipt_revision) == (source["message_revision"], receipt_revision)
        ask()
        assert len(delivered) == 1

        # Prove a committed source withdrawal alone fails closed, before
        # mutating OWNER membership and thereby invalidating the epoch anchor.
        assert client.post(REVOKE, json=revoke).status_code == 204
        with pytest.raises(ValueError, match="NO_ELIGIBLE_SOURCE"):
            ask()
        assert len(delivered) == 1

        # A subsequent OWNER loss must reject authority even for idempotent
        # revoke; restoring OWNER is not itself an epoch-reconciliation proof.
        with Session.begin() as db:
            membership = db.scalar(select(ClientTenantMembershipRow).where(
                ClientTenantMembershipRow.human_identity_id == "fixture-human-owner",
            ))
            membership.role = "MEMBER"
        assert client.post(REVOKE, json=revoke).status_code == 403
        with Session.begin() as db:
            db.get(ClientTenantMembershipRow, membership.id).role = "OWNER"
        assert client.post(REVOKE, json=revoke).status_code == 204
        assert client.post(GRANT, json=grant).status_code == 409

    assert len(delivered) == 1
