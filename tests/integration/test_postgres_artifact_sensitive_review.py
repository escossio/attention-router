"""Disposable PostgreSQL contract for Android visual-information review."""

from datetime import timedelta

import pytest
from sqlalchemy import inspect, select, text

from attention_router.application.artifact_sensitive_review import (
    register_artifact_sensitive_review,
)
from attention_router.application.client_approval import (
    ClientApprovalService,
)
from attention_router.config import Settings, settings
from attention_router.infrastructure.artifact_sensitive_review_models import ArtifactSensitiveReviewRow
from attention_router.infrastructure.models import (
    ExecutionIntentRow, HumanExecutionAuthorizationRow, OutboxMessageRow,
)
from tests.client.test_artifact_sensitive_review import (
    _seed_artifact,
)
from tests.client.test_client_approval_service import (
    NOW, HUMAN_ID, issue_session,
)


pytestmark = pytest.mark.postgres


def test_postgres_sensitive_review_schema_is_private_and_separate(Session):
    with Session() as session:
        schema = inspect(session.bind)
        assert "artifact_sensitive_reviews" in schema.get_table_names()
        columns = {c["name"] for c in schema.get_columns("artifact_sensitive_reviews")}
        assert {
            "tenant_id", "artifact_id", "understanding_id",
            "source_inbound_event_id", "category", "state",
            "expected_owner_human_identity_id", "decision_human_identity_id",
            "decision_device_id", "decision_client_session_id",
        } <= columns
        assert not columns.intersection({
            "raw_text", "extracted_text", "visual_description", "cvv",
            "card_number", "pan", "secret", "account_number", "password",
        })
        uniques = {
            x["name"] for x in schema.get_unique_constraints("artifact_sensitive_reviews")
        }
        checks = {
            x["name"] for x in schema.get_check_constraints("artifact_sensitive_reviews")
        }
        assert "uq_artifact_sensitive_review_source" in uniques
        assert "ck_artifact_sensitive_review_state" in checks
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0062_artifact_sensitive_review"
        )


def test_postgres_image_review_uses_existing_android_authority_without_effects(
    Session, monkeypatch,
):
    monkeypatch.setattr(settings, "artifact_sensitive_review_enabled", True)
    monkeypatch.setattr(
        settings, "artifact_sensitive_review_start_at", NOW-timedelta(days=1),
    )
    with Session() as session:
        service, issued = issue_session(session)
        understanding = _seed_artifact(session)
        review = register_artifact_sensitive_review(
            session, understanding=understanding,
            timestamp=NOW+timedelta(seconds=4),
        )
        assert review is not None and review.state == "PENDING"
        assert review.expected_owner_human_identity_id == HUMAN_ID
        session.commit()

        config = Settings(
            _env_file=None,
            app_env="test",
            admin_auth_enabled=False,
            internal_ingress_hmac_secret="x" * 32,
            client_session_enabled=True,
            client_session_ttl_seconds=900,
            client_approval_enabled=True,
            artifact_sensitive_review_enabled=True,
            artifact_sensitive_review_start_at=NOW-timedelta(days=1),
        )
        approval = ClientApprovalService(settings=config, client_sessions=service)
        pending = approval.list_pending(
            session, session_token=issued.session_token,
            now=NOW + timedelta(seconds=5),
        )
        assert len(pending) == 1
        assert pending[0].approval_id == review.id
        assert "0099" not in pending[0].preview

        decided = approval.decide(
            session, session_token=issued.session_token,
            approval_id=review.id, decision="APPROVE",
            now=NOW + timedelta(seconds=6),
        )
        session.commit()
        assert decided.state == "APPROVED"
        assert session.get(ArtifactSensitiveReviewRow, review.id).state == "APPROVED_PRIVATE"
        assert session.scalar(select(ExecutionIntentRow)) is None
        assert session.scalar(select(HumanExecutionAuthorizationRow)) is None
        assert session.scalar(select(OutboxMessageRow)) is None
