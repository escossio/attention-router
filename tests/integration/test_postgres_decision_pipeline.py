import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from attention_router.adapters.inbound import NormalizedInboundEvent
from attention_router.application import services
from attention_router.application.decision_pipeline import process_agent_decision
from attention_router.infrastructure.models import AgentBlueprintRow, AgentBlueprintVersionRow, AgentDecisionRow


pytestmark = pytest.mark.postgres


def test_postgres_agent_decision_persists_once(Session):
    now = datetime.now(timezone.utc)
    blueprint = AgentBlueprintRow(
        id=str(uuid.uuid4()),
        name="Postgres decision fixture",
        status="published",
        created_at=now,
        updated_at=now,
    )
    version = AgentBlueprintVersionRow(
        id=str(uuid.uuid4()),
        blueprint_id=blueprint.id,
        version=1,
        spec={"autonomy": {"level": "observe"}, "missing_information": [], "escalation": []},
        checksum=str(uuid.uuid4()),
        created_at=now,
        created_by="test",
        change_reason="test",
        is_immutable=True,
    )
    blueprint.current_version_id = version.id
    with Session() as session:
        session.add_all([blueprint, version])
        session.flush()
        event = NormalizedInboundEvent(
            source="postgres-test",
            external_event_id=f"event-{uuid.uuid4()}",
            event_type="message",
            actor_id="unknown-postgres-actor",
            actor_display_name="Unknown",
            actor_category="unknown",
            channel="test",
            content="Postgres decision fixture.",
        )
        interaction = services.receive_normalized_inbound_event(session, event)
        session.commit()
        first = process_agent_decision(session, interaction["inbound_event_id"])
        session.commit()
        second = process_agent_decision(session, interaction["inbound_event_id"])
        assert first.id == second.id
        assert first.execution_allowed is False
        assert first.external_delivery_allowed is False
        assert session.scalar(
            select(text("count(*)")).select_from(AgentDecisionRow).where(
                AgentDecisionRow.event_id == interaction["inbound_event_id"]
            )
        ) == 1
