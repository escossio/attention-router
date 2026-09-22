from datetime import UTC, datetime, timedelta
import base64
import hashlib

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from attention_router.application.client_approval import (
    ClientApprovalConflict,
    ClientApprovalService,
)
from attention_router.application.client_session import ClientSessionService
from attention_router.config import Settings
from attention_router.infrastructure.client_bootstrap_models import (
    ClientDeviceRow,
    ClientTenantMembershipRow,
)
from attention_router.infrastructure.db import Base
from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.models import (
    ExecutionIntentRow,
    HumanApprovalDecisionEvidenceRow,
    HumanExecutionAuthorizationRow,
    TenantRow,
)
from attention_router.platform.human_execution_authorization import (
    prepare,
    request_client_approval,
)
from attention_router.security.device_keys import encode_unpadded_base64url


NOW = datetime(2026, 9, 22, 6, 30, tzinfo=UTC)
HUMAN_ID = "hid_" + "h" * 24
TENANT_ID = "tnt_" + "t" * 24
DEVICE_ID = "cdev_" + "d" * 24
@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with Session() as value:
        yield value
    engine.dispose()


def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        admin_auth_enabled=False,
        internal_ingress_hmac_secret="x" * 32,
        client_session_enabled=True,
        client_session_challenge_ttl_seconds=300,
        client_session_ttl_seconds=900,
        client_approval_enabled=True,
    )


def keypair():
    private = ec.generate_private_key(ec.SECP256R1())
    spki = private.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, spki, encode_unpadded_base64url(spki)


def sign(private, challenge: str) -> str:
    raw = base64.urlsafe_b64decode(challenge + "=" * (-len(challenge) % 4))
    return encode_unpadded_base64url(
        private.sign(raw, ec.ECDSA(hashes.SHA256()))
    )


def seed_authority(session, spki: bytes):
    session.add(HumanIdentityRow(id=HUMAN_ID, created_at=NOW))
    session.add(
        TenantRow(
            id=TENANT_ID,
            slug="native-approval-proof",
            name="Personal",
            status="ACTIVE",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.flush()
    session.add(
        ClientTenantMembershipRow(
            id="ctm_" + "m" * 24,
            human_identity_id=HUMAN_ID,
            tenant_id=TENANT_ID,
            role="OWNER",
            status="ACTIVE",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.add(
        ClientDeviceRow(
            id=DEVICE_ID,
            human_identity_id=HUMAN_ID,
            public_key_fingerprint="sha256:" + hashlib.sha256(spki).hexdigest(),
            public_key_spki=spki,
            canonical_name="Synthetic Android",
            platform="ANDROID",
            roles=["CAPABILITY_NODE", "CLIENT"],
            status="ACTIVE",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.commit()


def issue_session(session):
    private, spki, public = keypair()
    seed_authority(session, spki)
    service = ClientSessionService(settings=settings())
    challenge = service.start_session(
        session,
        public_key_spki_b64url=public,
        requested_tenant_id=None,
        now=NOW + timedelta(seconds=1),
    )
    issued = service.complete_session(
        session,
        session_challenge_id=challenge.session_challenge_id,
        device_signature_b64url=sign(private, challenge.challenge_b64url),
        now=NOW + timedelta(seconds=2),
    )
    session.commit()
    return service, issued


def seed_approval(session, *, suffix="a"):
    scope = {
        "target": {
            "tenant": TENANT_ID,
            "transport": "meta_whatsapp",
            "canonical_address": "synthetic-target",
        },
        "frozen_authority": {
            "capability": "conversation.reply",
            "operation": "conversation.reply",
        },
        "immutable_inputs": {
            "effective_response_snapshot": "Mensagem proposta pela Andy",
        },
    }
    parent = ExecutionIntentRow(
        id=f"intent-{suffix}",
        idempotency_key=f"intent-key-{suffix}",
        scope=scope,
        scope_fingerprint=f"fingerprint-{suffix}",
        provenance={"source": "test"},
        state="FROZEN",
        created_at=NOW,
        frozen_at=NOW,
        retired_at=None,
        authority_profile_id=None,
        expires_at=NOW + timedelta(minutes=10),
    )
    session.add(parent)
    session.flush()
    row = prepare(
        session,
        execution_intent_id=parent.id,
        expected_approver=HUMAN_ID,
        ttl_seconds=300,
        correlation_id=f"corr-{suffix}",
        now=NOW + timedelta(seconds=3),
        approval_channel="android_client",
    )
    request_client_approval(
        session,
        row.id,
        now=NOW + timedelta(seconds=3),
    )
    session.commit()
    return row


def approval_service(session_service):
    return ClientApprovalService(
        settings=settings(),
        client_sessions=session_service,
    )


def test_lists_only_authenticated_pending_approval(session):
    session_service, issued = issue_session(session)
    row = seed_approval(session)
    service = approval_service(session_service)

    items = service.list_pending(
        session,
        session_token=issued.session_token,
        now=NOW + timedelta(seconds=4),
    )
    assert len(items) == 1
    assert items[0].approval_id == row.id
    assert items[0].capability == "conversation.reply"
    assert items[0].target == "synthetic-target"
    assert items[0].preview == "Mensagem proposta pela Andy"


def test_approve_persists_provider_neutral_android_evidence(session):
    session_service, issued = issue_session(session)
    row = seed_approval(session)
    service = approval_service(session_service)

    result = service.decide(
        session,
        session_token=issued.session_token,
        approval_id=row.id,
        decision="APPROVE",
        now=NOW + timedelta(seconds=4),
    )
    session.commit()

    evidence = session.scalar(
        select(HumanApprovalDecisionEvidenceRow).where(
            HumanApprovalDecisionEvidenceRow.authorization_id == row.id
        )
    )
    stored = session.get(HumanExecutionAuthorizationRow, row.id)
    assert result.state == "APPROVED"
    assert stored.state == "APPROVED"
    assert stored.decision_inbound_wamid is None
    assert stored.decision_sender is None
    assert evidence is not None
    assert evidence.channel == "ANDROID_CLIENT"
    assert evidence.decision == "APPROVE"
    assert evidence.human_identity_id == HUMAN_ID
    assert evidence.device_id == DEVICE_ID
    assert evidence.client_session_id is not None
    assert evidence.provider_event_reference is None


def test_same_android_decision_is_idempotent(session):
    session_service, issued = issue_session(session)
    row = seed_approval(session)
    service = approval_service(session_service)
    first = service.decide(
        session,
        session_token=issued.session_token,
        approval_id=row.id,
        decision="APPROVE",
        now=NOW + timedelta(seconds=4),
    )
    session.commit()
    parent = session.get(ExecutionIntentRow, row.execution_intent_id)
    parent.state = "MATERIALIZED"
    session.commit()
    second = service.decide(
        session,
        session_token=issued.session_token,
        approval_id=row.id,
        decision="APPROVE",
        now=NOW + timedelta(seconds=5),
    )
    session.commit()

    assert first.state == second.state == "APPROVED"
    assert len(
        list(session.scalars(select(HumanApprovalDecisionEvidenceRow)).all())
    ) == 1


def test_conflicting_second_decision_fails_closed(session):
    session_service, issued = issue_session(session)
    row = seed_approval(session)
    service = approval_service(session_service)

    service.decide(
        session,
        session_token=issued.session_token,
        approval_id=row.id,
        decision="APPROVE",
        now=NOW + timedelta(seconds=4),
    )
    session.commit()

    with pytest.raises(ClientApprovalConflict):
        service.decide(
            session,
            session_token=issued.session_token,
            approval_id=row.id,
            decision="DENY",
            now=NOW + timedelta(seconds=5),
        )


def test_cross_tenant_scope_is_not_listed(session):
    session_service, issued = issue_session(session)
    row = seed_approval(session)
    parent = session.get(ExecutionIntentRow, row.execution_intent_id)
    parent.scope = {
        **parent.scope,
        "target": {
            **parent.scope["target"],
            "tenant": "tnt_other",
        },
    }
    session.commit()

    service = approval_service(session_service)
    assert service.list_pending(
        session,
        session_token=issued.session_token,
        now=NOW + timedelta(seconds=4),
    ) == ()
