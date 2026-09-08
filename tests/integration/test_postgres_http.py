import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from attention_router.infrastructure.models import (
    AuditEventRow,
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
    TimerRow,
)
from attention_router.web.app import app, get_session


pytestmark = pytest.mark.postgres

HTTP_SCENARIO_FIXTURE: dict[str, str] = {}


@pytest.fixture()
def client(Session, monkeypatch):
    # The HTTP contract requires real synthetic lineage, not orphan foreign keys.
    from attention_router.infrastructure.models import ScenarioDefinitionRow, ScenarioVersionRow
    from tests.integration.test_postgres_10_5c_6i1a_r2_3b_legacy_synthetic import (
        _synthetic_run,
    )

    with Session() as session:
        run = _synthetic_run(session)
        version = session.get(ScenarioVersionRow, run.scenario_version_id)
        definition = session.get(ScenarioDefinitionRow, version.scenario_definition_id)
        monkeypatch.setitem(HTTP_SCENARIO_FIXTURE, "run_id", run.id)
        monkeypatch.setitem(HTTP_SCENARIO_FIXTURE, "scenario_id", definition.scenario_key)
        session.commit()
    def override():
        session = Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_session] = override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def payload(event_id: str, text_value: str = "Fixture HTTP sintética."):
    return {
        "schema_version": "synthetic-1",
        "synthetic_event_id": event_id,
        "event_type": "message",
        "contact_id": "contact_mae",
        "contact_name": "Mãe Sintética",
        "relationship_category": "family_core",
        "text": text_value,
        "metadata": {"case": "http"},
        "scenario_id": HTTP_SCENARIO_FIXTURE["scenario_id"],
        "scenario_run_id": HTTP_SCENARIO_FIXTURE["run_id"],
        "stimulus_id": f"stimulus-{event_id}",
    }


def test_http_synthetic_ingress_e2e_and_replay(Session, client):
    event_id = f"http-{uuid.uuid4()}"
    first = client.post("/api/v1/ingress/synthetic/events", json=payload(event_id))
    second = client.post("/api/v1/ingress/synthetic/events", json=payload(event_id))
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["interaction_id"] == second.json()["interaction_id"]
    interaction_id = first.json()["interaction_id"]
    with Session() as session:
        assert session.scalar(select(text("count(*)")).select_from(InboundEventRow).where(InboundEventRow.external_event_id == event_id)) == 1
        assert session.scalar(select(text("count(*)")).select_from(InteractionRow).where(InteractionRow.id == interaction_id)) == 1
        assert session.scalar(select(text("count(*)")).select_from(TimerRow).where(TimerRow.interaction_id == interaction_id)) == 1
        assert session.scalar(select(text("count(*)")).select_from(OutboxMessageRow).where(OutboxMessageRow.interaction_id == interaction_id)) == 1
        assert session.scalar(select(text("count(*)")).select_from(AuditEventRow).where(AuditEventRow.interaction_id == interaction_id)) > 0


def test_http_same_id_different_payload_conflicts(Session, client):
    event_id = f"conflict-{uuid.uuid4()}"
    assert client.post("/api/v1/ingress/synthetic/events", json=payload(event_id)).status_code == 200
    conflict = client.post("/api/v1/ingress/synthetic/events", json=payload(event_id, "Texto diferente."))
    assert conflict.status_code == 409
    with Session() as session:
        assert session.scalar(select(text("count(*)")).select_from(InboundEventRow).where(InboundEventRow.external_event_id == event_id)) == 1
        assert session.scalar(select(text("count(*)")).select_from(AuditEventRow).where(AuditEventRow.event_type == "duplicate_payload_conflict")) >= 1


def test_http_concurrent_synthetic_ingress_deduplicates(Session, client):
    event_id = f"http-race-{uuid.uuid4()}"
    responses = []

    def post_event():
        responses.append(client.post("/api/v1/ingress/synthetic/events", json=payload(event_id)))

    threads = [threading.Thread(target=post_event) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [response.status_code for response in responses] == [200, 200]
    interaction_ids = {response.json()["interaction_id"] for response in responses}
    assert len(interaction_ids) == 1
    with Session() as session:
        assert session.scalar(select(text("count(*)")).select_from(InboundEventRow).where(InboundEventRow.external_event_id == event_id)) == 1
        assert session.scalar(select(text("count(*)")).select_from(InteractionRow).where(InteractionRow.id.in_(interaction_ids))) == 1
