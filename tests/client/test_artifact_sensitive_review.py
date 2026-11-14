"""Synthetic proof of sensitivity review and Android approval reuse.

Never stores real card information, credentials or personal data.
"""

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from attention_router.application.artifact_sensitive_review import (
    classify_sensitive_artifact,
    register_artifact_sensitive_review,
)
from attention_router.application.client_approval import (
    ClientApprovalAuthorityRejected,
    ClientApprovalConflict,
    ClientApprovalService,
)
from attention_router.config import Settings, settings as live_settings
from attention_router.infrastructure import human_identity_models  # noqa: F401
from attention_router.infrastructure.artifact_models import ArtifactRow, ArtifactUnderstandingRow
from attention_router.infrastructure.artifact_sensitive_review_models import ArtifactSensitiveReviewRow
from attention_router.infrastructure.client_bootstrap_models import ClientTenantMembershipRow
from attention_router.infrastructure.db import Base
from attention_router.infrastructure.models import (
    ExecutionIntentRow, HumanExecutionAuthorizationRow, InboundEventRow,
    OutboxMessageRow, ResourceRow,
)
from tests.client.test_client_approval_service import (
    HUMAN_ID, TENANT_ID, NOW, issue_session, seed_approval,
)

SAMPLE = "Cartão bancário sintético. Número de teste mascarado XXXX 0099."


@pytest.fixture
def review_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with Factory() as session:
        yield session
    engine.dispose()


def _settings():
    return Settings(
        _env_file=None, app_env="test",
        admin_auth_enabled=False, internal_ingress_hmac_secret="x" * 32,
        client_session_enabled=True, client_session_ttl_seconds=900,
        client_approval_enabled=True, artifact_sensitive_review_enabled=True,
        artifact_sensitive_review_start_at=NOW-timedelta(days=1),
    )


def _seed_artifact(session, *, tenant=TENANT_ID, content=SAMPLE, kind="IMAGE"):
    now = NOW + timedelta(seconds=3)
    resource = ResourceRow(
        id="resource-one", tenant_id=tenant, resource_type="ARTIFACT",
        canonical_name="synthetic-image-object", status="ACTIVE",
        metadata_json={}, created_at=now, updated_at=now,
    )
    event = InboundEventRow(
        id="synthetic-receipt-one", tenant_id=tenant, source="wwebjs",
        external_event_id="image-synthetic-one", event_type="message",
        payload={
            "channel": "whatsapp", "event_origin": "EXTERNAL_INBOUND",
            "metadata": {"message_type": "image", "has_media": True},
        },
        payload_hash="synthetic-hash", received_at=now,
        status="PROCESSED", correlation_id="synthetic-correlation",
        lineage_classification="ORGANIC",
    )
    artifact = ArtifactRow(
        id="artifact-one", tenant_id=tenant, resource_id=resource.id,
        content_sha256="a" * 64, artifact_kind="IMAGE",
        mime_type="image/png", size_bytes=48, storage_provider="local-fs-v1",
        storage_reference="sha256:" + "a" * 64, original_filename=None,
        status="AVAILABLE", metadata_json={},
        created_at=now, updated_at=now,
    )
    understanding = ArtifactUnderstandingRow(
        id="understanding-one", tenant_id=tenant, artifact_id=artifact.id,
        inbound_event_id=event.id, status="READY", content_kind=kind,
        summary_text=content, extracted_text="",
        visual_description="Foto de um objeto de teste.",
        key_facts_json=[], language="pt-BR", text_truncated=False,
        provider="synthetic", model="synthetic",
        prompt_version="artifact_understanding_v1",
        attempt_count=1, created_at=now, updated_at=now, completed_at=now,
    )
    session.add_all([resource, event, artifact, understanding])
    session.flush()
    return understanding


def test_classifier_recognizes_sensitive_visual_data_without_copying_values():
    cases = {
        "Cartão de crédito Visa": "PAYMENT_CARD",
        "Cartão bancário": "PAYMENT_CARD",
        "CPF: documento de identificação fictício": "IDENTITY_DOCUMENT",
        "Carteira Nacional de Habilitação": "IDENTITY_DOCUMENT",
        "Senha de acesso escrita no papel": "ACCESS_SECRET",
        "Cartão de visita, João, empresa sintética": "CONTACT_CARD",
    }
    for phrase, category in cases.items():
        assert classify_sensitive_artifact(
            summary=phrase, extracted_text="", visual_description="", key_facts=[],
        ) == category
    assert classify_sensitive_artifact(
        summary="Uma cadeira ao lado da janela.",
        extracted_text="PROMOÇÃO R$ 42",
        visual_description="", key_facts=[],
    ) is None


def test_sensitive_image_queues_one_masked_native_approval_with_no_execution(
    review_session, monkeypatch,
):
    session = review_session
    session_service, issued = issue_session(session)
    monkeypatch.setattr(live_settings, "artifact_sensitive_review_enabled", True)
    monkeypatch.setattr(
        live_settings, "artifact_sensitive_review_start_at", NOW-timedelta(days=1),
    )
    understanding = _seed_artifact(session)
    created = register_artifact_sensitive_review(
        session, understanding=understanding, timestamp=NOW+timedelta(seconds=4),
    )
    assert created is not None and created.state == "PENDING"
    assert created.expected_owner_human_identity_id == HUMAN_ID
    assert created.category == "PAYMENT_CARD"
    assert register_artifact_sensitive_review(
        session, understanding=understanding, timestamp=NOW+timedelta(seconds=5),
    ).id == created.id
    session.commit()

    approval_service = ClientApprovalService(
        settings=_settings(), client_sessions=session_service,
    )
    visible = approval_service.list_pending(
        session, session_token=issued.session_token,
        now=NOW+timedelta(seconds=6),
    )
    assert len(visible) == 1
    request = visible[0]
    assert request.approval_id == created.id
    assert request.state == "PENDING_HUMAN_APPROVAL"
    assert "cartão de pagamento" in request.preview
    assert "0099" not in request.preview
    assert "XXXX" not in request.preview
    assert "Nenhuma ação externa" in request.preview
    assert "0099" not in str(vars(created))
    assert session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 0
    assert session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 0

    result = approval_service.decide(
        session, session_token=issued.session_token, approval_id=created.id,
        decision="APPROVE", now=NOW+timedelta(seconds=7),
    )
    session.commit()
    assert result.state == "APPROVED"
    assert created.state == "APPROVED_PRIVATE"
    assert created.decision_human_identity_id == HUMAN_ID
    assert created.decision_device_id is not None
    assert created.decision_client_session_id is not None
    assert approval_service.list_pending(
        session, session_token=issued.session_token,
        now=NOW+timedelta(seconds=8),
    ) == ()
    assert approval_service.decide(
        session, session_token=issued.session_token, approval_id=created.id,
        decision="APPROVE", now=NOW+timedelta(seconds=9),
    ).state == "APPROVED"
    with pytest.raises(ClientApprovalConflict):
        approval_service.decide(
            session, session_token=issued.session_token, approval_id=created.id,
            decision="DENY", now=NOW+timedelta(seconds=9),
        )
    assert session.scalar(select(func.count()).select_from(HumanExecutionAuthorizationRow)) == 0
    assert session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 0
    assert session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 0


def test_deny_preserves_original_artifact_but_does_not_grant_memory_use(
    review_session, monkeypatch,
):
    session_service, issued = issue_session(review_session)
    monkeypatch.setattr(live_settings, "artifact_sensitive_review_enabled", True)
    monkeypatch.setattr(
        live_settings, "artifact_sensitive_review_start_at", NOW-timedelta(days=1),
    )
    understanding = _seed_artifact(review_session, content="Cartão de visita de teste.")
    request = register_artifact_sensitive_review(
        review_session, understanding=understanding, timestamp=NOW+timedelta(seconds=4),
    )
    assert request is not None and request.category == "CONTACT_CARD"
    service = ClientApprovalService(
        settings=_settings(), client_sessions=session_service,
    )
    result = service.decide(
        review_session, session_token=issued.session_token, approval_id=request.id,
        decision="DENY", now=NOW+timedelta(seconds=6),
    )
    review_session.commit()
    assert result.state == "DENIED"
    assert request.state == "ARCHIVE_ONLY"
    assert review_session.get(ArtifactRow, understanding.artifact_id).status == "AVAILABLE"
    assert review_session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 0


def test_unknown_owner_or_non_image_never_fabricates_app_approval(
    review_session, monkeypatch,
):
    _service, _issued = issue_session(review_session)
    monkeypatch.setattr(live_settings, "artifact_sensitive_review_enabled", True)
    monkeypatch.setattr(
        live_settings, "artifact_sensitive_review_start_at", NOW-timedelta(days=1),
    )
    understanding = _seed_artifact(review_session, content="Foto comum.")
    assert register_artifact_sensitive_review(
        review_session, understanding=understanding, timestamp=NOW+timedelta(seconds=4),
    ) is None
    assert review_session.scalar(select(func.count()).select_from(ArtifactSensitiveReviewRow)) == 0


def test_non_owner_cannot_approve_artifact_and_scope_is_preserved(
    review_session, monkeypatch,
):
    session_service, issued = issue_session(review_session)
    monkeypatch.setattr(live_settings, "artifact_sensitive_review_enabled", True)
    monkeypatch.setattr(
        live_settings, "artifact_sensitive_review_start_at", NOW-timedelta(days=1),
    )
    understanding = _seed_artifact(review_session)
    request = register_artifact_sensitive_review(
        review_session, understanding=understanding, timestamp=NOW+timedelta(seconds=4),
    )
    owner = review_session.scalar(select(ClientTenantMembershipRow))
    owner.role = "MEMBER"
    review_session.commit()
    service = ClientApprovalService(settings=_settings(), client_sessions=session_service)
    assert service.list_pending(
        review_session, session_token=issued.session_token,
        now=NOW+timedelta(seconds=6),
    ) == ()
    with pytest.raises(ClientApprovalAuthorityRejected):
        service.decide(
            review_session, session_token=issued.session_token,
            approval_id=request.id, decision="APPROVE",
            now=NOW+timedelta(seconds=6),
        )
    assert request.state == "PENDING"


def test_gate_disabled_never_creates_review(review_session, monkeypatch):
    _service, _issued = issue_session(review_session)
    monkeypatch.setattr(live_settings, "artifact_sensitive_review_enabled", False)
    row = _seed_artifact(review_session)
    assert register_artifact_sensitive_review(
        review_session, understanding=row, timestamp=NOW+timedelta(seconds=4),
    ) is None

def test_existing_execution_approval_remains_independent(
    review_session, monkeypatch,
):
    session_service, issued = issue_session(review_session)
    execution = seed_approval(review_session, suffix="execution-with-review")
    monkeypatch.setattr(live_settings, "artifact_sensitive_review_enabled", True)
    monkeypatch.setattr(
        live_settings, "artifact_sensitive_review_start_at", NOW-timedelta(days=1),
    )
    understanding = _seed_artifact(review_session)
    review = register_artifact_sensitive_review(
        review_session, understanding=understanding,
        timestamp=NOW + timedelta(seconds=4),
    )
    assert review is not None
    service = ClientApprovalService(
        settings=_settings(), client_sessions=session_service,
    )
    visible = service.list_pending(
        review_session, session_token=issued.session_token,
        now=NOW + timedelta(seconds=5),
    )
    assert len(visible) == 2
    assert {x.approval_id for x in visible} == {review.id, execution.id}
    assert service.decide(
        review_session, session_token=issued.session_token,
        approval_id=review.id, decision="APPROVE",
        now=NOW + timedelta(seconds=6),
    ).state == "APPROVED"
    assert execution.state == "PENDING_HUMAN_APPROVAL"
    assert review.state == "APPROVED_PRIVATE"
    assert review_session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 1
    assert service.list_pending(
        review_session, session_token=issued.session_token,
        now=NOW + timedelta(seconds=7),
    )[0].approval_id == execution.id


def test_sensitive_review_expires_without_promoting_data(
    review_session, monkeypatch,
):
    from attention_router.application.client_approval import ClientApprovalExpired

    session_service, issued = issue_session(review_session)
    monkeypatch.setattr(live_settings, "artifact_sensitive_review_enabled", True)
    monkeypatch.setattr(
        live_settings, "artifact_sensitive_review_start_at", NOW-timedelta(days=1),
    )
    understanding = _seed_artifact(review_session)
    review = register_artifact_sensitive_review(
        review_session, understanding=understanding,
        timestamp=NOW + timedelta(seconds=3),
    )
    assert review is not None
    service = ClientApprovalService(
        settings=_settings(), client_sessions=session_service,
    )
    # Simulate expiry within an otherwise active client session.
    review.expires_at = NOW + timedelta(seconds=5)
    review_session.flush()
    assert service.list_pending(
        review_session, session_token=issued.session_token,
        now=NOW + timedelta(seconds=6),
    ) == ()
    with pytest.raises(ClientApprovalExpired):
        service.decide(
            review_session, session_token=issued.session_token,
            approval_id=review.id, decision="APPROVE",
            now=NOW + timedelta(seconds=6),
        )
    assert review.state == "PENDING"
    assert review_session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 0
