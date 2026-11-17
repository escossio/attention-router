"""Reconciled #404/#408 chain; disposable database and synthetic records only."""
from datetime import timedelta

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from attention_router.application.artifact_sensitive_review import register_artifact_sensitive_review
from attention_router.config import settings
from attention_router.infrastructure.client_location_history_models import ClientLocationObservationRow
from attention_router.infrastructure.client_bootstrap_models import ClientDeviceRow
from tests.client.test_artifact_sensitive_review import _seed_artifact
from tests.client.test_client_approval_service import NOW, HUMAN_ID, TENANT_ID, issue_session
from tests.integration.test_postgres_candidate_insight_migration import (
    candidate_insight_migration_db,  # noqa: F401
)

pytestmark = pytest.mark.postgres


def test_reconciled_chain_preserves_data_and_refuses_loss(candidate_insight_migration_db, monkeypatch):  # noqa: F811
    engine, migrate = candidate_insight_migration_db
    migrate('downgrade', '0061_tts_outbox_constraint')
    with Session(engine) as session:
        issue_session(session)
        understanding = _seed_artifact(session)
        original_id = understanding.id
        original_text = understanding.summary_text
        session.commit()
    migrate('upgrade', '0062_artifact_sensitive_review')
    monkeypatch.setattr(settings, 'artifact_sensitive_review_enabled', True)
    monkeypatch.setattr(settings, 'artifact_sensitive_review_start_at', NOW-timedelta(days=1))
    from attention_router.infrastructure.artifact_models import ArtifactUnderstandingRow
    with Session(engine) as session:
        understanding = session.get(ArtifactUnderstandingRow, original_id)
        assert understanding.summary_text == original_text
        review = register_artifact_sensitive_review(session, understanding=understanding,
                                                    timestamp=NOW+timedelta(seconds=4))
        review_id = review.id
        assert review.state == 'PENDING'
        session.commit()
    migrate('upgrade', 'head')
    with Session(engine) as session:
        assert session.scalar(text('SELECT version_num FROM alembic_version')) == '0063_location_context_v1'
        assert session.scalar(text('SELECT count(*) FROM client_location_observations')) == 0
        assert session.get(ArtifactUnderstandingRow, original_id).summary_text == original_text
        device = session.scalar(select(ClientDeviceRow).where(ClientDeviceRow.human_identity_id == HUMAN_ID))
        session.add(ClientLocationObservationRow(id='synthetic-chain-position', tenant_id=TENANT_ID,
            human_identity_id=HUMAN_ID, device_id=device.id, latitude=10, longitude=20,
            accuracy_m=25, precision='APPROXIMATE', captured_at=NOW, received_at=NOW))
        session.commit()
    refused = migrate('downgrade', '0062_artifact_sensitive_review', check=False)
    assert refused.returncode != 0
    assert 'LOCATION_HISTORY_ROLLBACK_REQUIRES_EXPLICIT_EXPORT_OR_PURGE' in refused.stderr
    with engine.begin() as connection:
        assert connection.scalar(text('SELECT count(*) FROM client_location_observations')) == 1
        assert connection.scalar(text('SELECT id FROM artifact_sensitive_reviews')) == review_id
        # Only this isolated test's synthetic row; never a production rollback recipe.
        connection.execute(text("DELETE FROM client_location_observations WHERE id='synthetic-chain-position'"))
    migrate('downgrade', '0062_artifact_sensitive_review')
    assert 'client_location_observations' not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.scalar(text('SELECT id FROM artifact_sensitive_reviews')) == review_id
    refused = migrate('downgrade', '0061_tts_outbox_constraint', check=False)
    assert refused.returncode != 0
    assert 'ARTIFACT_SENSITIVE_REVIEW_DOWNGRADE_REQUIRES_EXPLICIT_ARCHIVE' in refused.stderr
    with engine.begin() as connection:
        assert connection.scalar(text('SELECT id FROM artifact_sensitive_reviews')) == review_id
        connection.execute(text('DELETE FROM artifact_sensitive_reviews WHERE id=:id'), {'id':review_id})
    migrate('downgrade', '0061_tts_outbox_constraint')
    migrate('upgrade', 'head')
    with Session(engine) as session:
        assert session.get(ArtifactUnderstandingRow, original_id).summary_text == original_text
        assert session.scalar(text('SELECT count(*) FROM client_location_observations')) == 0
        assert session.scalar(text('SELECT count(*) FROM artifact_sensitive_reviews')) == 0
