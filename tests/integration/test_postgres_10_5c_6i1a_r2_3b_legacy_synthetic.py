"""R2.3B behavioral compatibility proof for legacy and synthetic paths."""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from attention_router.application import autonomy, response_review
from attention_router.application.repetition import RepetitionDecision
from attention_router.config import settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import (
    ActorBindingRow,
    AgentBlueprintRow,
    AgentBlueprintVersionRow,
    AgentDecisionRow,
    AgentExecutionIntentRow,
    ExecutionIntentRow,
    InboundEventRow,
    InteractionRow,
    PolicyVersionRow,
    ScenarioRunRow,
)
from attention_router.platform.l0_executor import execute_l0_manifest
from attention_router.platform.production_authority import ProductionAuthorityDenied
from attention_router.platform.production_bridge import create_production_scenario_run
from attention_router.platform.scenarios import create_scenario_run, load_manifest_file
from tests.integration.test_postgres_10_5c_6i1a_r2_bridge_adversarial import (
    _decision,
    _legacy_scenario_version,
    _production_agent,
)


pytestmark = pytest.mark.postgres


def _production_counts(session) -> tuple[int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(ExecutionIntentRow)),
        session.scalar(
            select(func.count()).select_from(AgentExecutionIntentRow).where(
                AgentExecutionIntentRow.execution_intent_id.is_not(None)
            )
        ),
        session.scalar(
            select(func.count()).select_from(ScenarioRunRow).where(
                ScenarioRunRow.agent_execution_intent_id.is_not(None)
            )
        ),
    )


def _response_review_intent(session) -> AgentExecutionIntentRow:
    decision_id = _decision(session)
    review = response_review.create_review_for_decision(session, decision_id)
    response_review.edit_review(session, review.id, "R2.3B reviewed response")
    approved, intent = response_review.approve_review(session, review.id)
    assert approved.status == "APPROVED"
    assert intent.response_review_id == review.id
    return intent


def _autonomy_decision(session) -> AgentDecisionRow:
    suffix = uuid4().hex
    identities = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures/synthetic-identities.json").read_text()
    )
    peer = identities["ACTOR_A"]["jid"]
    now = datetime.now(UTC)
    blueprint = AgentBlueprintRow(
        id=f"r23b-blueprint-{suffix}",
        tenant_id=DEFAULT_TENANT_ID,
        name="R2.3B autonomy fixture",
        status="published",
        created_at=now,
        updated_at=now,
    )
    session.add(blueprint)
    session.flush()
    version = AgentBlueprintVersionRow(
        id=f"r23b-blueprint-version-{suffix}",
        blueprint_id=blueprint.id,
        version=1,
        spec={
            "autonomy": {"execution_mode": "AUTO_ALLOWED"},
            "missing_information": [],
            "escalation": [],
        },
        checksum=f"r23b-blueprint-checksum-{suffix}",
        created_at=now,
        created_by="r2.3b",
        change_reason="legacy compatibility proof",
        is_immutable=True,
    )
    session.add(version)
    session.flush()
    blueprint.current_version_id = version.id

    policy = PolicyVersionRow(
        id=f"r23b-policy-version-{suffix}",
        policy_id="desconhecido",
        version=int(suffix[:7], 16),
        config={
            "allowed_actions": ["soft_ping"],
            "execution_mode": "AUTO_ALLOWED",
            "execution_scope": {"audiences": ["canary"]},
        },
        checksum=f"r23b-policy-checksum-{suffix}",
        created_at=now,
        created_by="r2.3b",
        is_immutable=True,
    )
    interaction = InteractionRow(
        id=f"r23b-interaction-{suffix}",
        tenant_id=DEFAULT_TENANT_ID,
        event_type="message",
        contact_id=peer,
        contact_name="R2.3B Canary",
        relationship_category="canary",
        inbound_text="hello",
        state="active",
        policy_id="desconhecido",
        policy_version_id=policy.id,
        correlation_id=f"r23b-correlation-{suffix}",
        created_at=now,
        updated_at=now,
    )
    event = InboundEventRow(
        id=f"r23b-event-{suffix}",
        tenant_id=DEFAULT_TENANT_ID,
        source="wwebjs",
        external_event_id=f"r23b-external-{suffix}",
        event_type="message",
        payload={
            "external_actor_id": peer, "channel": "whatsapp", "event_type": "message",
            "event_origin": "EXTERNAL_INBOUND", "owner_authenticated": False,
            "metadata": {
                "from_me": False, "conversation_state": "READY", "is_group": False,
                "conversation_key": f"wwebjs:{peer}", "source_account": "test-account",
                "peer_identifiers": [peer], "peer_id_kind": "c.us",
            },
        },
        lineage_classification="ORGANIC",
        payload_hash=f"r23b-payload-{suffix}",
        received_at=now,
        interaction_id=interaction.id,
        status="processed",
        correlation_id=interaction.correlation_id,
    )
    decision = AgentDecisionRow(
        id=f"r23b-decision-{suffix}",
        event_id=event.id,
        interaction_id=interaction.id,
        agent_blueprint_id=blueprint.id,
        agent_blueprint_version=1,
        policy_version_id=policy.id,
        audience="canary",
        decision_pipeline_version="r2.3b",
        decision_type="RESPOND",
        recommended_action="soft_ping",
        proposed_response="R2.3B autonomous response",
        escalation_required=False,
        confidence=0.9,
        missing_information=[],
        execution_allowed=False,
        external_delivery_allowed=False,
        reasoning_summary="legacy compatibility proof",
        status="DRY_RUN",
        created_at=now,
    )
    session.add(policy)
    session.flush()
    session.add(interaction)
    session.flush()
    session.add(event)
    session.flush()
    session.add(decision)
    session.flush()
    return decision


def _autonomy_intent(session, monkeypatch) -> AgentExecutionIntentRow:
    decision = _autonomy_decision(session)
    monkeypatch.setattr(settings, "autonomous_execution_enabled", True)
    monkeypatch.setattr(settings, "external_delivery_enabled", True)
    monkeypatch.setattr(
        settings,
        "autonomous_execution_activated_at",
        (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    monkeypatch.setattr(
        autonomy,
        "suppress_if_repeated",
        lambda *args, **kwargs: RepetitionDecision(False, None, "r2.3b"),
    )
    evaluation = autonomy.evaluate_and_route(session, decision)
    assert evaluation.automatic_execution_allowed is True
    intent = session.scalar(
        select(AgentExecutionIntentRow).where(
            AgentExecutionIntentRow.autonomy_evaluation_id == evaluation.id
        )
    )
    assert intent is not None
    assert intent.authorization_source == "POLICY_AUTONOMY"
    return intent


def _legacy_agent(session) -> AgentExecutionIntentRow:
    suffix = uuid4().hex
    row = AgentExecutionIntentRow(
        id=f"r23b-legacy-agent-{suffix}",
        agent_decision_id=_decision(session),
        authorization_source="HUMAN_REVIEW",
        intent_type="WHATSAPP_RESPONSE",
        effective_response_snapshot="legacy response",
        status="BLOCKED",
        execution_allowed=False,
        external_delivery_allowed=False,
        release_status="HELD",
        idempotency_key=f"r23b-legacy-key-{suffix}",
        created_at=datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


def _synthetic_actor(session) -> ActorBindingRow:
    existing = session.scalar(
        select(ActorBindingRow).where(
            ActorBindingRow.tenant_id == DEFAULT_TENANT_ID,
            ActorBindingRow.actor_key == "actor_synthetic_test_actor",
            ActorBindingRow.is_active.is_(True),
        ).limit(1)
    )
    if existing is not None:
        return existing
    suffix = uuid4().hex
    now = datetime.now(UTC)
    actor = ActorBindingRow(
        id=f"r23b-synthetic-actor-{suffix}",
        tenant_id=DEFAULT_TENANT_ID,
        source=f"r23b-synthetic-driver-{suffix}",
        external_actor_id=f"r23b-synthetic-target-{suffix}",
        actor_key="actor_synthetic_test_actor",
        display_name="R2.3B synthetic fixture",
        actor_category="SYNTHETIC_TEST_ACTOR",
        active_context=None,
        is_active=True,
        binding_metadata={
            "synthetic": True,
            "lineage_classification": "SYNTHETIC",
            "stimulus_id": f"r23b-stimulus-{suffix}",
        },
        created_at=now,
        updated_at=now,
    )
    session.add(actor)
    session.flush()
    return actor


def _synthetic_run(session) -> ScenarioRunRow:
    actor = _synthetic_actor(session)
    version = _legacy_scenario_version(session)
    now = datetime.now(UTC)
    run = create_scenario_run(
        session,
        run_id=f"r23b-synthetic-run-{uuid4().hex}",
        tenant_id=DEFAULT_TENANT_ID,
        scenario_version_id=version.id,
        synthetic_actor_binding_id=actor.id,
        agent_execution_intent_id=None,
        root_correlation_id=f"r23b-synthetic-correlation-{uuid4().hex}",
        source_sha="r2.3b",
        runtime_sha="r2.3b",
        schema_revision="0026",
        driver_revision="synthetic-v1",
        readiness_result_id=None,
        effect_budget_id=None,
        expires_at=now + timedelta(minutes=5),
        require_synthetic_actor=True,
        now=now,
    )
    return run


def _execute_synthetic_v1(session) -> ScenarioRunRow:
    _synthetic_actor(session)
    manifest_path = Path("config/platform/scenarios/SCN-PE-001.yaml")
    manifest = load_manifest_file(manifest_path)
    result = execute_l0_manifest(
        session,
        manifest=manifest,
        manifest_path=str(manifest_path),
        source_sha="r2.3b",
        runtime_sha="r2.3b",
        schema_revision="0026",
        driver_revision="synthetic-v1",
    )
    assert result.status == "PASSED"
    return session.get(ScenarioRunRow, result.run_id)


def test_lr01_response_review_legacy(Session):
    with Session() as session:
        before = _production_counts(session)
        intent = _response_review_intent(session)
        session.commit()
        assert intent.execution_intent_id is None
        assert intent.execution_intent_fingerprint is None
    with Session() as observer:
        stored = observer.get(AgentExecutionIntentRow, intent.id)
        assert stored is not None
        assert stored.execution_intent_id is None
        assert stored.execution_intent_fingerprint is None
        assert _production_counts(observer) == before


def test_lr02_autonomy_legacy(Session, monkeypatch):
    with Session() as session:
        before = _production_counts(session)
        intent = _autonomy_intent(session, monkeypatch)
        session.commit()
        assert intent.status == "READY"
        assert intent.release_status == "RELEASED"
        assert intent.execution_intent_id is None
        assert intent.execution_intent_fingerprint is None
    with Session() as observer:
        stored = observer.get(AgentExecutionIntentRow, intent.id)
        assert stored.execution_intent_id is None
        assert stored.execution_intent_fingerprint is None
        assert _production_counts(observer) == before


def test_lr03_legacy_agent_null_bridge(Session):
    with Session() as session:
        legacy = _legacy_agent(session)
        session.commit()
        legacy_id = legacy.id
    with Session() as observer:
        stored = observer.get(AgentExecutionIntentRow, legacy_id)
        assert stored is not None
        assert stored.execution_intent_id is None
        assert stored.execution_intent_fingerprint is None


def test_lr04_legacy_scenario_run(Session):
    with Session() as session:
        version = _legacy_scenario_version(session)
        now = datetime.now(UTC)
        run = create_scenario_run(
            session,
            run_id=f"r23b-legacy-run-{uuid4().hex}",
            tenant_id=DEFAULT_TENANT_ID,
            scenario_version_id=version.id,
            synthetic_actor_binding_id=None,
            agent_execution_intent_id=None,
            root_correlation_id=f"r23b-legacy-correlation-{uuid4().hex}",
            source_sha="r2.3b",
            runtime_sha="r2.3b",
            schema_revision="0026",
            driver_revision=None,
            readiness_result_id=None,
            effect_budget_id=None,
            expires_at=now + timedelta(minutes=5),
            require_synthetic_actor=False,
            now=now,
        )
        session.commit()
        run_id = run.id
    with Session() as observer:
        stored = observer.get(ScenarioRunRow, run_id)
        assert stored is not None
        assert stored.agent_execution_intent_id is None


def test_lr05_synthetic_v1(Session):
    with Session() as session:
        run = _execute_synthetic_v1(session)
        session.commit()
        run_id = run.id
    with Session() as observer:
        stored = observer.get(ScenarioRunRow, run_id)
        assert stored.synthetic_actor_binding_id is not None
        assert stored.agent_execution_intent_id is None


def test_lr06_synthetic_no_production_promotion(Session):
    with Session() as session:
        _, production_agent_id = _production_agent(session)
        synthetic_run = _synthetic_run(session)
        session.commit()
        run_id = synthetic_run.id
        with pytest.raises(DBAPIError) as rejected:
            session.execute(
                text(
                    "UPDATE scenario_runs SET agent_execution_intent_id=:agent "
                    "WHERE id=:run"
                ),
                {"agent": production_agent_id, "run": run_id},
            )
        assert "production scenario run bridge is immutable" in str(rejected.value)
        session.rollback()
    with Session() as observer:
        assert observer.get(ScenarioRunRow, run_id).agent_execution_intent_id is None


def test_lr07_production_run(Session):
    with Session() as session:
        parent, production_agent_id = _production_agent(session)
        run = create_production_scenario_run(
            session,
            agent_execution_intent_id=production_agent_id,
            tenant_id=DEFAULT_TENANT_ID,
            scenario_version_id=parent.scope["scenario"]["id"],
            run_id=f"r23b-production-run-{uuid4().hex}",
            root_correlation_id=f"r23b-production-correlation-{uuid4().hex}",
            expires_at=parent.expires_at,
        )
        session.commit()
        run_id = run.id
    with Session() as observer:
        stored = observer.get(ScenarioRunRow, run_id)
        assert stored.agent_execution_intent_id == production_agent_id
        assert stored.synthetic_actor_binding_id is None


def test_lr08_production_rejects_legacy_agent(Session):
    with Session() as session:
        legacy = _legacy_agent(session)
        version = _legacy_scenario_version(session)
        session.commit()
        with pytest.raises(
            ProductionAuthorityDenied,
            match="PRODUCTION_AGENT_PARENT_REQUIRED",
        ):
            create_production_scenario_run(
                session,
                agent_execution_intent_id=legacy.id,
                tenant_id=DEFAULT_TENANT_ID,
                scenario_version_id=version.id,
                run_id=f"r23b-rejected-run-{uuid4().hex}",
                root_correlation_id=f"r23b-rejected-correlation-{uuid4().hex}",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        session.rollback()


def test_lr09_no_silent_legacy_capture(Session, monkeypatch):
    with Session() as session:
        before = _production_counts(session)
        reviewed = _response_review_intent(session)
        autonomous = _autonomy_intent(session, monkeypatch)
        synthetic = _execute_synthetic_v1(session)
        session.commit()
        assert reviewed.execution_intent_id is None
        assert reviewed.execution_intent_fingerprint is None
        assert autonomous.execution_intent_id is None
        assert autonomous.execution_intent_fingerprint is None
        assert synthetic.agent_execution_intent_id is None
    with Session() as observer:
        assert _production_counts(observer) == before
