"""Offline checks for the persistent consent model and its eligibility predicate."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from attention_router.application.context_consent import RETENTION_LAYERS, SourceConsent
from attention_router.application.context_pg_consent import PgSourceConsent, _digest, _matches
from attention_router.infrastructure.artifact_models import ArtifactReceiptRow
from attention_router.infrastructure.context_consent_models import ContextConsentScopeRow, ContextSourceConsentRow
from attention_router.infrastructure.models import ConversationMessageRow

NOW = datetime(2026, 11, 23, tzinfo=UTC)


@pytest.fixture
def source():
    grant = SourceConsent(
        "decision", "tenant", "owner", "account", "conversation", "message", "receipt",
        "prospective_context", NOW, NOW + timedelta(hours=2), "NORMAL",
        tuple((layer, NOW + timedelta(hours=1)) for layer in sorted(RETENTION_LAYERS)),
    )
    message = ConversationMessageRow(
        id="message", tenant_id="tenant", source_account="account", source="wwebjs",
        conversation_id="conversation", sent_at=NOW, sensitivity_class="NORMAL",
        context_revision="m-v1", text="Synthetic evidence", content_hash="fixture", searchable=True,
        metadata_json={"artifact_receipt_id": "receipt", "artifact_id": "artifact"},
    )
    receipt = ArtifactReceiptRow(id="receipt", artifact_id="artifact", context_revision="r-v1", metadata_json={})
    row = ContextSourceConsentRow(
        message_revision="m-v1", receipt_revision="r-v1", receipt_id="receipt", content_digest=_digest(message),
    )
    return row, grant, message, receipt


def test_matching_source_is_eligible(source):
    assert _matches(*source, owner_id="owner", now=NOW, required_layers=("embedding",))


@pytest.mark.parametrize(("index", "field", "value"), [
    (0, "revoked_at", NOW),
    (2, "tenant_id", "foreign"),
    (2, "source_account", "foreign"),
    (2, "conversation_id", "foreign"),
    (2, "context_revision", "m-v2"),
    (2, "text", "Replacement text"),
    (2, "sensitivity_class", "SENSITIVE"),
    (2, "metadata_json", {"artifact_receipt_id": "other", "artifact_id": "artifact"}),
    (3, "context_revision", "r-v2"),
    (3, "metadata_json", {"context_evidence_withdrawn": True}),
])
def test_ineligible_source(source, index, field, value):
    setattr(source[index], field, value)
    assert not _matches(*source, owner_id="owner", now=NOW)


def test_owner_and_deadlines(source):
    assert not _matches(*source, owner_id="other", now=NOW)
    assert not _matches(*source, owner_id="owner", now=NOW + timedelta(hours=1))
    assert not _matches(*source, owner_id="owner", now=NOW, required_layers=("unknown",))


def test_model_requires_complete_decision_and_accepts_tombstone():
    engine = create_engine("sqlite://")
    ContextConsentScopeRow.__table__.create(engine)
    ContextSourceConsentRow.__table__.create(engine)
    with Session(engine) as session, session.begin():
        session.add(ContextConsentScopeRow(
            id="scope", tenant_id="tenant", owner_id="owner", binding_id="binding", source_account="account",
            resource_id="resource", purpose="prospective_context", scope_start=NOW, incarnation="fixture",
        ))
    with Session(engine) as session:
        with pytest.raises(ValueError, match="CONTEXT_PG_CONSENT_UNAVAILABLE"), session.begin():
            PgSourceConsent().current(session, None)
        with pytest.raises(IntegrityError), session.begin():
            session.add(ContextSourceConsentRow(scope_id="scope", message_id="message"))
        with session.begin():
            session.add(ContextSourceConsentRow(scope_id="scope", message_id="message", revoked_at=NOW))
        assert session.get(ContextConsentScopeRow, "scope").revision == 0
    engine.dispose()
