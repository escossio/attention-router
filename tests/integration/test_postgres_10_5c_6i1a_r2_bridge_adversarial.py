"""Adversarial PostgreSQL proof for the production authority bridge.

These tests deliberately exercise the database constraints and the T1
transaction.  They use the integration PostgreSQL fixture, never SQLite.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import hashlib
import json
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from attention_router.infrastructure.models import (
    AgentDecisionRow,
    AgentExecutionIntentRow,
    BoundedRunAuthorizationRow,
    EffectBudgetRow,
    EffectConsumptionRow,
    ExecutionClassVersionRow,
    ExecutionIntentRow,
    ExecutionIntentTargetBindingRow,
    HumanExecutionAuthorizationRow,
    InboundEventRow,
    MetaDeliveryReconciliationRow,
    OutboxMessageRow,
    PolicyRow,
    PolicyVersionRow,
    RecipientEndpointRow,
    SafetySetVersionRow,
    ScenarioDefinitionRow,
    ScenarioRunRow,
    ScenarioVersionAuthorityBindingRow,
    ScenarioVersionRow,
    StaticIntentAuthorityProfileRow,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.platform.human_execution_authorization import fingerprint
from attention_router.platform.meta_callback_reconciliation import (
    admit_meta_callback_evidence,
    reconcile_meta_callback_outcome,
)
from attention_router.platform.production_authority import (
    FrozenAuthority,
    production_scope_material,
    semantic_scope_fingerprint,
)
from attention_router.platform.production_bridge import (
    create_production_scenario_run,
    materialize_approved_production_agent_intent,
    materialize_production_agent_intent,
)
from attention_router.platform.production_controlled_execution import (
    mark_production_meta_api_accepted,
    prepare_production_controlled_execution,
    reserve_production_conversation_reply,
)

pytestmark = pytest.mark.postgres


def _decision(session, suffix=None):
    suffix = suffix or uuid4().hex
    now = datetime.now(UTC)
    interaction_id, event_id, decision_id = (f"r2-{suffix}-{x}" for x in ("i", "e", "d"))
    session.execute(text("""INSERT INTO interactions
        (id, tenant_id, event_type, contact_id, contact_name, relationship_category,
         inbound_text, state, correlation_id, created_at, updated_at)
        VALUES (:id, :tenant, 'message', 'contact', 'Contact', 'unknown', 'hello',
                'active', :corr, :now, :now)"""),
        {"id": interaction_id, "tenant": DEFAULT_TENANT_ID, "corr": f"r2-{suffix}", "now": now})
    session.execute(text("""INSERT INTO inbound_events
        (id, tenant_id, source, external_event_id, event_type, payload, payload_hash,
         received_at, interaction_id, status, correlation_id)
        VALUES (:id, :tenant, 'r2', :external, 'message', :payload, :hash,
                :now, :interaction, 'processed', :corr)"""),
        {"id": event_id, "tenant": DEFAULT_TENANT_ID, "external": f"r2-{suffix}",
         "payload": '{"channel":"test"}', "hash": suffix, "now": now,
         "interaction": interaction_id, "corr": f"r2-{suffix}"})
    session.flush()
    session.add(AgentDecisionRow(
        id=decision_id, event_id=event_id, interaction_id=interaction_id,
        decision_pipeline_version="r2", decision_type="RESPOND", recommended_action="reply",
        proposed_response="ok", escalation_required=False, confidence=1,
        missing_information=[], execution_allowed=False, external_delivery_allowed=False,
        reasoning_summary="fixture", status="DRY_RUN", created_at=now,
    ))
    session.flush()
    return decision_id


def _parent(session, suffix=None):
    suffix = suffix or uuid4().hex
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=5)
    scenario = _legacy_scenario_version(
        session, suffix, environment_classification="PRODUCTION"
    )
    execution_class = ExecutionClassVersionRow(
        id=f"r2-class-{suffix}", identity=f"r2-class-{suffix}", version=1,
        environment_classification="PRODUCTION", status="ACTIVE",
        allowed_transports=["meta_whatsapp"],
        allowed_operations=["conversation.reply"],
        allowed_capabilities=["conversation.reply"],
        external_effect_class="CONTROLLED_EXTERNAL_MESSAGE",
        max_target_cardinality=1, requires_human_authorization=True,
        requires_fresh_readiness=True, requires_handoff=True,
        requires_bounded_authorization=True, direct_execution_allowed=False,
        created_at=now, is_immutable=True,
    )
    session.add(execution_class)
    session.flush()
    safety = SafetySetVersionRow(
        id=f"r2-safety-{suffix}", identity=f"r2-safety-{suffix}", version=1,
        environment_classification="PRODUCTION", status="ACTIVE",
        execution_class_version_id=execution_class.id,
        allowed_transport="meta_whatsapp", allowed_operation="conversation.reply",
        allowed_capability="conversation.reply", max_target_cardinality=1,
        max_outbound_messages=1, max_action_count=1,
        required_invariants=["single-recipient", "human-approved"],
        created_at=now, is_immutable=True,
    )
    profile = StaticIntentAuthorityProfileRow(
        id=f"r2-profile-{suffix}", identity=f"r2-profile-{suffix}", version=1,
        environment_classification="PRODUCTION", status="ACTIVE",
        max_target_cardinality=1, max_outbound_messages=1, max_action_count=1,
        max_retries=0, allowed_transport="meta_whatsapp",
        allowed_operation="conversation.reply", allowed_capability="conversation.reply",
        created_at=now, is_immutable=True,
    )
    if session.get(PolicyRow, "desconhecido") is None:
        session.add(PolicyRow(
            identifier="desconhecido", tenant_id=DEFAULT_TENANT_ID,
            name="R2 production authority fixture", match_criteria={}, priority=0,
            specificity=0, tone="neutral", initial_wait_seconds=0,
            allowed_disclosures=[], allowed_actions=["conversation.reply"],
            escalation_steps=[], ack_timeout_seconds=0, repetition_limit=1,
            cancellation_conditions=[], completion_conditions=[],
            current_version_id=None, is_active=True,
        ))
        session.flush()
    policy = PolicyVersionRow(
        id=f"r2-policy-{suffix}", policy_id="desconhecido",
        version=int(suffix[:7], 16), config={"allowed_actions": ["conversation.reply"]},
        checksum=f"r2-policy-checksum-{suffix}", created_at=now,
        created_by="r2", is_immutable=True,
        environment_classification="PRODUCTION", status="ACTIVE",
    )
    session.add_all([safety, profile, policy])
    session.flush()
    session.add_all([
        ScenarioVersionAuthorityBindingRow(
            scenario_version_id=scenario.id, binding_role="EXECUTION_CLASS",
            execution_class_version_id=execution_class.id, created_at=now,
        ),
        ScenarioVersionAuthorityBindingRow(
            scenario_version_id=scenario.id, binding_role="SAFETY_SET",
            safety_set_version_id=safety.id, created_at=now,
        ),
        ScenarioVersionAuthorityBindingRow(
            scenario_version_id=scenario.id, binding_role="POLICY",
            policy_version_id=policy.id, created_at=now,
        ),
        ScenarioVersionAuthorityBindingRow(
            scenario_version_id=scenario.id, binding_role="AUTHORITY_PROFILE",
            authority_profile_id=profile.id, created_at=now,
        ),
    ])
    address = f"55{int(suffix[:12], 16)}"
    endpoint = RecipientEndpointRow(
        id=f"r2-endpoint-{suffix}", tenant_id=DEFAULT_TENANT_ID,
        transport="meta_whatsapp", canonical_address=address,
        status="ACTIVE", created_at=now,
    )
    session.add(endpoint)
    session.flush()
    authority = FrozenAuthority(
        target_count=1, outbound_messages=1, action_count=1, retries=0,
        transport="meta_whatsapp", operation="conversation.reply",
        capability="conversation.reply", expires_at=expires_at,
        target_identity=(f"{DEFAULT_TENANT_ID}|meta_whatsapp|{address}",),
    )
    scope = production_scope_material(
        scenario_identity={
            "id": scenario.id, "version": scenario.version,
            "content_hash": scenario.content_hash,
        },
        execution_class={
            "id": execution_class.id, "identity": execution_class.identity,
            "version": execution_class.version,
        },
        safety_set={"id": safety.id, "identity": safety.identity, "version": safety.version},
        policies=[{
            "id": policy.id, "policy_id": policy.policy_id,
            "version": policy.version, "checksum": policy.checksum,
        }],
        authority_profile={"id": profile.id, "identity": profile.identity, "version": profile.version},
        frozen_authority=authority, tenant_identity=DEFAULT_TENANT_ID,
        canonical_address=address,
        audience={
            "audience_type": "single_represented_owner_contact",
            "audience_id": "owner",
        },
        immutable_inputs={"effective_response_snapshot": "ok"},
    )
    parent = ExecutionIntentRow(
        id=f"r2-parent-{suffix}", idempotency_key=f"r2-key-{suffix}", scope=scope,
        scope_fingerprint=semantic_scope_fingerprint(scope),
        provenance={"source": "test"}, state="PREPARED",
        created_at=now, frozen_at=None, authority_profile_id=profile.id,
        expires_at=expires_at,
    )
    session.add(parent)
    session.flush()
    session.add(ExecutionIntentTargetBindingRow(
        id=f"r2-target-{suffix}", execution_intent_id=parent.id,
        target_type="WHATSAPP_RECIPIENT_ENDPOINT",
        recipient_endpoint_id=endpoint.id, ordinal=0, created_at=now,
    ))
    session.flush()
    parent.state = "FROZEN"
    parent.frozen_at = now
    session.flush()
    session.add(HumanExecutionAuthorizationRow(
        id=f"r2-hea-{suffix}", execution_intent_id=parent.id,
        execution_intent_fingerprint=parent.scope_fingerprint, scope=scope,
        scope_fingerprint=parent.scope_fingerprint, expected_approver="owner",
        approval_channel="test", state="APPROVED", issued_at=now,
        expires_at=now + timedelta(minutes=5), correlation_id=f"r2-hea-c-{suffix}",
        created_at=now, updated_at=now,
    ))
    session.flush()
    return parent


def _legacy_scenario_version(
    session, suffix=None, *, environment_classification="SYNTHETIC"
):
    suffix = suffix or uuid4().hex
    now = datetime.now(UTC)
    definition = ScenarioDefinitionRow(
        id=f"r2-sd-{suffix}", tenant_id=DEFAULT_TENANT_ID,
        scenario_key=f"r2-{suffix}", title="R2 legacy", description="fixture",
        created_at=now, updated_at=now)
    session.add(definition)
    session.flush()
    version = ScenarioVersionRow(
        id=f"r2-sv-{suffix}", tenant_id=DEFAULT_TENANT_ID,
        scenario_definition_id=definition.id, version=1, schema_version="r2",
        manifest_source_path="tests/r2.yaml", content_hash=suffix,
        requirements_covered=[], risk_ids=[], config_keys=[], risk_classification="LOW",
        enabled=True, source_sha="r2", created_at=now, is_immutable=True,
        environment_classification=environment_classification)
    session.add(version)
    session.flush()
    return version


def _materialize(session, parent, decision_id, expected=None):
    row = materialize_production_agent_intent(
        session, execution_intent_id=parent.id,
        expected_fingerprint=expected or parent.scope_fingerprint,
        agent_decision_id=decision_id, effective_response_snapshot="ok",
    )
    session.commit()
    return row.id


def test_r2_t01_atomic_materialization_and_replay(Session):
    with Session() as session:
        parent = _parent(session)
        decision = _decision(session)
        projection_id = _materialize(session, parent, decision)
        assert session.get(ExecutionIntentRow, parent.id).state == "MATERIALIZED"
        assert session.scalar(select(func.count()).select_from(AgentExecutionIntentRow).where(
            AgentExecutionIntentRow.execution_intent_id == parent.id)) == 1
        assert _materialize(session, parent, decision) == projection_id


def test_r2_t01a_materializes_from_real_approved_meta_lineage(Session):
    with Session() as session:
        parent = _parent(session)
        authorization = session.scalar(select(HumanExecutionAuthorizationRow).where(
            HumanExecutionAuthorizationRow.execution_intent_id == parent.id
        ))
        authorization.request_wamid = f"request-{parent.id}"
        authorization.decision_inbound_wamid = f"decision-{parent.id}"
        authorization.decision_sender = authorization.expected_approver
        authorization.decision_button_id = "approve"
        authorization.decision_at = datetime.now(UTC)
        shadow_payload = {
            "sender": authorization.decision_sender,
            "message_type": "interactive",
            "interactive_type": "button_reply",
            "button_reply_id": authorization.decision_button_id,
            "context_id": authorization.request_wamid,
            "normalization": "PASS",
            "dispatch_enabled": False,
        }
        session.add(
            InboundEventRow(
                id="r2-shadow-" + uuid4().hex,
                tenant_id=DEFAULT_TENANT_ID,
                source="meta_whatsapp_shadow",
                external_event_id=authorization.decision_inbound_wamid,
                event_type="shadow_normalized",
                payload=shadow_payload,
                payload_hash=hashlib.sha256(
                    json.dumps(
                        shadow_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                received_at=authorization.decision_at,
                processed_at=authorization.decision_at,
                interaction_id=None,
                status="SHADOW_NORMALIZED",
                error=None,
                correlation_id=authorization.correlation_id,
                lineage_classification="ORGANIC",
            )
        )
        session.flush()

        projection = materialize_approved_production_agent_intent(
            session,
            execution_intent_id=parent.id,
            authorization_id=authorization.id,
            expected_fingerprint=parent.scope_fingerprint,
            effective_response_snapshot="ok",
        )
        replay = materialize_approved_production_agent_intent(
            session,
            execution_intent_id=parent.id,
            authorization_id=authorization.id,
            expected_fingerprint=parent.scope_fingerprint,
            effective_response_snapshot="ok",
        )
        assert replay.id == projection.id
        assert session.get(ExecutionIntentRow, parent.id).state == "MATERIALIZED"
        assert session.get(AgentDecisionRow, projection.agent_decision_id).semantic_source == "approved_execution_intent"


def test_r2_t01b_one_bounded_production_reply_reaches_terminal_state(Session):
    with Session() as session:
        parent = _parent(session)
        decision = _decision(session)
        authorization = session.scalar(select(HumanExecutionAuthorizationRow).where(
            HumanExecutionAuthorizationRow.execution_intent_id == parent.id
        ))
        authorization.decision_at = datetime.now(UTC)
        agent = materialize_production_agent_intent(
            session,
            execution_intent_id=parent.id,
            expected_fingerprint=parent.scope_fingerprint,
            agent_decision_id=decision,
            effective_response_snapshot="ok",
        )
        prepared = prepare_production_controlled_execution(
            session,
            agent_execution_intent_id=agent.id,
            authorization_id=authorization.id,
            run_id=f"run-{uuid4().hex}",
            root_correlation_id=uuid4().hex,
            lease_ttl_seconds=120,
        )
        reserved = reserve_production_conversation_reply(
            session,
            agent_execution_intent_id=agent.id,
            authorization_id=authorization.id,
            dispatcher_id="test-live-owner",
        )
        mark_production_meta_api_accepted(
            session,
            outbox_message_id=reserved.outbox_message_id,
            dispatcher_id="test-live-owner",
            provider_message_id="wamid.test",
        )
        session.commit()
        reconciliation = session.scalar(
            select(MetaDeliveryReconciliationRow).where(
                MetaDeliveryReconciliationRow.outbox_message_id
                == reserved.outbox_message_id
            )
        )
        admit_meta_callback_evidence(
            Session,
            status_event={
                "id": "wamid.test",
                "status": "delivered",
                "timestamp": "1788220800",
                "errors": [],
            },
            received_at=reconciliation.deadline_at - timedelta(seconds=1),
        )
        reconcile_meta_callback_outcome(
            Session,
            reconciliation_id=reconciliation.id,
            now=reconciliation.deadline_at,
        )
        session.expire_all()

        run = session.get(ScenarioRunRow, prepared.scenario_run_id)
        outbox = session.get(OutboxMessageRow, reserved.outbox_message_id)
        budget = session.get(EffectBudgetRow, prepared.effect_budget_id)
        bounded = session.get(BoundedRunAuthorizationRow, prepared.bounded_authorization_id)
        consumption = session.get(EffectConsumptionRow, reserved.consumption_id)
        assert (run.status, outbox.status, agent.status) == ("PASSED", "DONE", "SENT")
        assert (budget.status, budget.consumed_count, consumption.state) == ("CONSUMED", 1, "CONSUMED")
        assert bounded.status == "CONSUMED"


def test_r2_fingerprint_control():
    base = {"scenario_version": "r2", "target": {"transport": "meta_whatsapp", "address": "x"},
            "operation": "conversation.reply", "capability": "conversation.reply"}
    assert fingerprint(base) == fingerprint(dict(base))
    changed = {**base, "target": {"transport": "meta_whatsapp", "address": "y"}}
    assert fingerprint(base) != fingerprint(changed)


@pytest.mark.parametrize("fault", ["after_insert", "after_update"])
def test_r2_t02_transaction_rolls_back_on_fault(Session, monkeypatch, fault):
    with Session() as session:
        parent = _parent(session)
        decision = _decision(session)
        session.commit()
        parent_id = parent.id
        expected_parent_fingerprint = parent.scope_fingerprint
        original_flush = session.flush
        calls = {"n": 0}

        def failing_flush(*args, **kwargs):
            calls["n"] += 1
            result = original_flush(*args, **kwargs)
            if (fault == "after_insert" and calls["n"] >= 2) or (fault == "after_update" and calls["n"] >= 3):
                raise RuntimeError("R2 injected transaction fault")
            return result

        monkeypatch.setattr(session, "flush", failing_flush)
        with pytest.raises(RuntimeError):
            materialize_production_agent_intent(
                session, execution_intent_id=parent_id,
                expected_fingerprint=expected_parent_fingerprint, agent_decision_id=decision,
                effective_response_snapshot="ok",
            )
        session.rollback()
    with Session() as observer:
        assert observer.get(ExecutionIntentRow, parent_id).state == "FROZEN"
        assert observer.scalar(select(func.count()).select_from(AgentExecutionIntentRow).where(
            AgentExecutionIntentRow.execution_intent_id == parent_id)) == 0


def test_t02a_rolls_back_after_projection_insert_fault(Session, monkeypatch):
    test_r2_t02_transaction_rolls_back_on_fault(Session, monkeypatch, "after_insert")


def test_t02b_rolls_back_after_parent_materialized_update_fault(Session, monkeypatch):
    test_r2_t02_transaction_rolls_back_on_fault(Session, monkeypatch, "after_update")


def test_f00_fixture_graph_builds_successfully(Session):
    with Session() as session:
        parent = _parent(session)
        decision = _decision(session)
        session.commit()
    with Session() as observer:
        assert observer.get(ExecutionIntentRow, parent.id) is not None
        assert observer.get(AgentDecisionRow, decision) is not None


def test_f01_materialization_fixture_satisfies_service_preconditions(Session):
    with Session() as session:
        parent = _parent(session)
        decision = _decision(session)
        assert parent.state == "FROZEN"
        assert parent.expires_at > datetime.now(UTC)
        assert session.scalar(select(func.count()).select_from(HumanExecutionAuthorizationRow).where(
            HumanExecutionAuthorizationRow.execution_intent_id == parent.id,
            HumanExecutionAuthorizationRow.state == "APPROVED")) == 1
        assert session.get(AgentDecisionRow, decision) is not None


def test_f02_legacy_bridge_fixtures_are_valid(Session):
    with Session() as session:
        decision = _decision(session)
        agent = AgentExecutionIntentRow(
            id=f"legacy-{uuid4().hex}", agent_decision_id=decision,
            intent_type="WHATSAPP_RESPONSE", effective_response_snapshot="ok",
            idempotency_key=f"legacy-key-{uuid4().hex}", created_at=datetime.now(UTC))
        session.add(agent)
        session.flush()
        assert agent.execution_intent_id is None
        assert agent.execution_intent_fingerprint is None
        session.commit()


def test_t04_materialization_replay_returns_same_projection(Session):
    test_r2_t01_atomic_materialization_and_replay(Session)


def test_t05_rejects_replay_with_different_fingerprint(Session):
    with Session() as session:
        parent = _parent(session)
        decision = _decision(session)
        projection_id = _materialize(session, parent, decision)
        with pytest.raises(Exception):
            materialize_production_agent_intent(
                session, execution_intent_id=parent.id,
                expected_fingerprint="different-fingerprint", agent_decision_id=decision,
                effective_response_snapshot="ok")
        session.rollback()
        assert session.get(AgentExecutionIntentRow, projection_id).execution_intent_fingerprint == parent.scope_fingerprint


def test_t06_supported_service_paths_preserve_materialized_projection_invariant(Session):
    with Session() as session:
        parent = _parent(session)
        decision = _decision(session)
        _materialize(session, parent, decision)
        assert parent.state == "MATERIALIZED"
        assert session.scalar(select(func.count()).select_from(AgentExecutionIntentRow).where(
            AgentExecutionIntentRow.execution_intent_id == parent.id)) == 1


def _db_rejects(session, statement, params):
    with pytest.raises(Exception):
        session.execute(text(statement), params)
    session.rollback()


def test_d01_rejects_duplicate_agent_execution_parent(Session):
    with Session() as session:
        parent = _parent(session)
        decision = _decision(session)
        first = _materialize(session, parent, decision)
        _db_rejects(session, "INSERT INTO agent_execution_intents (id, agent_decision_id, authorization_source, intent_type, effective_response_snapshot, status, execution_allowed, external_delivery_allowed, release_status, idempotency_key, created_at, execution_intent_id, execution_intent_fingerprint) SELECT :id, agent_decision_id, authorization_source, intent_type, effective_response_snapshot, status, execution_allowed, external_delivery_allowed, release_status, :key, created_at, execution_intent_id, execution_intent_fingerprint FROM agent_execution_intents WHERE id=:source", {"id": uuid4().hex, "key": uuid4().hex, "source": first})


def test_d02_rejects_nonexistent_execution_intent_parent(Session):
    with Session() as session:
        decision = _decision(session)
        _db_rejects(session, "INSERT INTO agent_execution_intents (id, agent_decision_id, authorization_source, intent_type, effective_response_snapshot, status, execution_allowed, external_delivery_allowed, release_status, idempotency_key, created_at, execution_intent_id, execution_intent_fingerprint) VALUES (:id,:d,'x','x','x','BLOCKED',false,false,'HELD',:key,NOW(),'missing-parent','fp')", {"id": uuid4().hex, "d": decision, "key": uuid4().hex})


def test_d03_rejects_duplicate_scenario_run_agent_parent(Session):
    with Session() as session:
        parent = _parent(session)
        decision = _decision(session)
        agent_id = _materialize(session, parent, decision)
        _db_rejects(session, "INSERT INTO scenario_runs (id,tenant_id,scenario_version_id,agent_execution_intent_id,status,root_correlation_id,source_sha,schema_revision,created_at,expires_at,updated_at) VALUES (:id,:tenant,:sv,:agent,'CREATED',:corr,'test','test',NOW(),NOW()+interval '5 minutes',NOW())", {"id": uuid4().hex, "tenant": "00000000-0000-4000-8000-000000000001", "sv": "missing-scenario", "agent": agent_id, "corr": uuid4().hex})


def test_d04_rejects_nonexistent_agent_parent_for_scenario_run(Session):
    with Session() as session:
        _db_rejects(session, "INSERT INTO scenario_runs (id,tenant_id,scenario_version_id,agent_execution_intent_id,status,root_correlation_id,source_sha,schema_revision,created_at,expires_at,updated_at) VALUES (:id,:tenant,:sv,'missing-agent','CREATED',:corr,'test','test',NOW(),NOW()+interval '5 minutes',NOW())", {"id": uuid4().hex, "tenant": "00000000-0000-4000-8000-000000000001", "sv": "missing-scenario", "corr": uuid4().hex})


def test_d05_allows_multiple_legacy_agent_intents_with_null_bridge(Session):
    with Session() as session:
        decision = _decision(session)
        for _ in range(2):
            session.add(AgentExecutionIntentRow(id=uuid4().hex, agent_decision_id=decision, intent_type="x", effective_response_snapshot="x", idempotency_key=uuid4().hex, created_at=datetime.now(UTC)))
            session.flush()


def test_d06_allows_multiple_legacy_scenario_runs_with_null_bridge(Session):
    with Session() as session:
        version = _legacy_scenario_version(session)
        now = datetime.now(UTC)
        for _ in range(2):
            session.add(ScenarioRunRow(
                id=uuid4().hex, tenant_id=DEFAULT_TENANT_ID,
                scenario_version_id=version.id, synthetic_actor_binding_id=None,
                agent_execution_intent_id=None, status="CREATED",
                root_correlation_id=uuid4().hex, source_sha="r2", schema_revision="r2",
                created_at=now, expires_at=now + timedelta(minutes=5), updated_at=now))
        session.commit()
    with Session() as observer:
        assert observer.scalar(select(func.count()).select_from(ScenarioRunRow).where(
            ScenarioRunRow.scenario_version_id == version.id,
            ScenarioRunRow.agent_execution_intent_id.is_(None))) == 2


def test_d07_rejects_parent_without_fingerprint(Session):
    with Session() as session:
        parent = _parent(session)
        decision = _decision(session)
        _db_rejects(session, "INSERT INTO agent_execution_intents (id,agent_decision_id,authorization_source,intent_type,effective_response_snapshot,status,execution_allowed,external_delivery_allowed,release_status,idempotency_key,created_at,execution_intent_id,execution_intent_fingerprint) VALUES (:id,:d,'x','x','x','BLOCKED',false,false,'HELD',:key,NOW(),:p,NULL)", {"id": uuid4().hex, "d": decision, "key": uuid4().hex, "p": parent.id})


def test_d08_rejects_fingerprint_without_parent(Session):
    with Session() as session:
        decision = _decision(session)
        _db_rejects(session, "INSERT INTO agent_execution_intents (id,agent_decision_id,authorization_source,intent_type,effective_response_snapshot,status,execution_allowed,external_delivery_allowed,release_status,idempotency_key,created_at,execution_intent_id,execution_intent_fingerprint) VALUES (:id,:d,'x','x','x','BLOCKED',false,false,'HELD',:key,NOW(),NULL,'fp')", {"id": uuid4().hex, "d": decision, "key": uuid4().hex})


def _production_agent(session):
    parent = _parent(session)
    decision = _decision(session)
    agent_id = _materialize(session, parent, decision)
    return parent, agent_id


def _run(session, agent_id=None, suffix=None):
    suffix = suffix or uuid4().hex
    version = _legacy_scenario_version(session, suffix)
    now = datetime.now(UTC)
    row = ScenarioRunRow(id=f"r2-run-{suffix}", tenant_id=DEFAULT_TENANT_ID,
        scenario_version_id=version.id, agent_execution_intent_id=agent_id,
        synthetic_actor_binding_id=None, status="CREATED", root_correlation_id=f"r2-c-{suffix}",
        source_sha="r2", schema_revision="r2", created_at=now,
        expires_at=now + timedelta(minutes=5), updated_at=now)
    session.add(row)
    session.flush()
    return row


def _reject_update(session, sql, params):
    with pytest.raises(Exception):
        session.execute(text(sql), params)
    session.rollback()


def test_i_bridge_01a_domain_agent_parent_rebind_is_not_exposed(Session):
    with Session() as session:
        parent_a, agent_a = _production_agent(session)
        parent_b, agent_b = _production_agent(session)
        assert agent_a != agent_b
        assert _materialize(session, parent_b, session.get(AgentExecutionIntentRow, agent_b).agent_decision_id) == agent_b
        stored_a = session.get(AgentExecutionIntentRow, agent_a)
        stored_b = session.get(AgentExecutionIntentRow, agent_b)
        assert stored_a.execution_intent_id == parent_a.id
        assert stored_b.execution_intent_id == parent_b.id


def test_i_bridge_01b_sql_agent_parent_rebind_rejected(Session):
    with Session() as session:
        a, agent_id = _production_agent(session)
        b = _parent(session)
        _reject_update(session, "UPDATE agent_execution_intents SET execution_intent_id=:b WHERE id=:id", {"b": b.id, "id": agent_id})


def test_i_bridge_01c_sql_agent_parent_removal_rejected(Session):
    with Session() as session:
        _, agent_id = _production_agent(session)
        _reject_update(session, "UPDATE agent_execution_intents SET execution_intent_id=NULL WHERE id=:id", {"id": agent_id})


def test_i_bridge_01d_legacy_agent_promotion_rejected(Session):
    with Session() as session:
        parent = _parent(session)
        decision = _decision(session)
        legacy = AgentExecutionIntentRow(id=uuid4().hex, agent_decision_id=decision, intent_type="x", effective_response_snapshot="x", idempotency_key=uuid4().hex, created_at=datetime.now(UTC))
        session.add(legacy)
        session.flush()
        _reject_update(session, "UPDATE agent_execution_intents SET execution_intent_id=:p, execution_intent_fingerprint=:f WHERE id=:id", {"p": parent.id, "f": parent.scope_fingerprint, "id": legacy.id})


def test_i_bridge_01e_sql_agent_fingerprint_mutation_rejected(Session):
    with Session() as session:
        _, agent_id = _production_agent(session)
        _reject_update(session, "UPDATE agent_execution_intents SET execution_intent_fingerprint='changed' WHERE id=:id", {"id": agent_id})


def test_i_bridge_01f_sql_agent_fingerprint_removal_rejected(Session):
    with Session() as session:
        _, agent_id = _production_agent(session)
        _reject_update(session, "UPDATE agent_execution_intents SET execution_intent_fingerprint=NULL WHERE id=:id", {"id": agent_id})


def test_i_bridge_02a_domain_run_rebind_is_not_exposed(Session):
    with Session() as session:
        parent_a, agent_a = _production_agent(session)
        parent_b, agent_b = _production_agent(session)
        run_a = create_production_scenario_run(
            session, agent_execution_intent_id=agent_a, tenant_id=DEFAULT_TENANT_ID,
            scenario_version_id=parent_a.scope["scenario"]["id"], run_id=uuid4().hex,
            root_correlation_id=uuid4().hex, expires_at=parent_a.expires_at)
        run_b = create_production_scenario_run(
            session, agent_execution_intent_id=agent_b, tenant_id=DEFAULT_TENANT_ID,
            scenario_version_id=parent_b.scope["scenario"]["id"], run_id=uuid4().hex,
            root_correlation_id=uuid4().hex, expires_at=parent_b.expires_at)
        session.commit()
        assert run_a.id != run_b.id
        assert run_a.agent_execution_intent_id == agent_a
        assert run_b.agent_execution_intent_id == agent_b


def test_i_bridge_02b_sql_run_rebind_rejected(Session):
    with Session() as session:
        _, a = _production_agent(session)
        _, b = _production_agent(session)
        run = _run(session, a)
        _reject_update(session, "UPDATE scenario_runs SET agent_execution_intent_id=:b WHERE id=:id", {"b": b, "id": run.id})


def test_i_bridge_02c_sql_run_removal_rejected(Session):
    with Session() as session:
        _, a = _production_agent(session)
        run = _run(session, a)
        _reject_update(session, "UPDATE scenario_runs SET agent_execution_intent_id=NULL WHERE id=:id", {"id": run.id})


def test_i_bridge_02d_legacy_run_promotion_rejected(Session):
    with Session() as session:
        _, agent_id = _production_agent(session)
        run = _run(session)
        _reject_update(session, "UPDATE scenario_runs SET agent_execution_intent_id=:agent WHERE id=:id", {"agent": agent_id, "id": run.id})


def test_x01_cross_tenant_agent_projection_is_not_implicitly_bindable(Session):
    with Session() as session:
        parent, agent_id = _production_agent(session)
        with pytest.raises(Exception):
            create_production_scenario_run(
                session, agent_execution_intent_id=agent_id, tenant_id="tenant-b",
                scenario_version_id="missing", run_id=uuid4().hex,
                root_correlation_id=uuid4().hex,
                expires_at=datetime.now(UTC) + timedelta(minutes=5))
        assert session.get(AgentExecutionIntentRow, agent_id).execution_intent_id == parent.id


def test_x02_cross_tenant_run_context_cannot_change_parent_link(Session):
    with Session() as session:
        parent, agent_id = _production_agent(session)
        version = _legacy_scenario_version(session)
        with pytest.raises(Exception):
            create_production_scenario_run(
                session, agent_execution_intent_id=agent_id, tenant_id="tenant-b",
                scenario_version_id=version.id, run_id=uuid4().hex,
                root_correlation_id=uuid4().hex,
                expires_at=datetime.now(UTC) + timedelta(minutes=5))
        assert parent.state == "MATERIALIZED"


@pytest.mark.parametrize("race_iteration", range(20))
def test_s01_concurrent_scenario_run_singleton_constraint_is_present(Session, race_iteration):
    with Session() as session:
        parent, agent_id = _production_agent(session)
        session.commit()
        version_id = parent.scope["scenario"]["id"]
        expires_at = parent.expires_at
    barrier = Barrier(2)
    def worker(_):
        with Session() as session:
            barrier.wait()
            try:
                row = create_production_scenario_run(
                    session, agent_execution_intent_id=agent_id,
                    tenant_id=DEFAULT_TENANT_ID, scenario_version_id=version_id,
                    run_id=f"r2-singleton-run-{race_iteration}",
                    root_correlation_id=f"r2-singleton-correlation-{race_iteration}",
                    expires_at=expires_at)
                session.commit()
                return row.id
            except Exception:
                session.rollback()
                with Session() as retry:
                    return retry.scalar(select(ScenarioRunRow.id).where(
                        ScenarioRunRow.agent_execution_intent_id == agent_id))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, range(2)))
    assert results[0] == results[1]
    with Session() as observer:
        assert observer.scalar(select(func.count()).select_from(ScenarioRunRow).where(
            ScenarioRunRow.agent_execution_intent_id == agent_id)) == 1


def test_s02_scenario_run_replay_lookup_is_singleton(Session):
    with Session() as session:
        parent, agent_id = _production_agent(session)
        run_id = uuid4().hex
        correlation_id = uuid4().hex
        first = create_production_scenario_run(
            session, agent_execution_intent_id=agent_id, tenant_id=DEFAULT_TENANT_ID,
            scenario_version_id=parent.scope["scenario"]["id"], run_id=run_id,
            root_correlation_id=correlation_id, expires_at=parent.expires_at)
        session.commit()
        with Session() as replay_session:
            second = create_production_scenario_run(
                replay_session, agent_execution_intent_id=agent_id, tenant_id=DEFAULT_TENANT_ID,
                scenario_version_id=parent.scope["scenario"]["id"], run_id=run_id,
                root_correlation_id=correlation_id, expires_at=parent.expires_at)
            replay_session.commit()
            assert second.id == first.id
            assert replay_session.scalar(select(func.count()).select_from(ScenarioRunRow).where(
                ScenarioRunRow.agent_execution_intent_id == agent_id)) == 1


def test_s03_different_agent_does_not_reuse_existing_run(Session):
    with Session() as session:
        parent_a, a = _production_agent(session)
        parent_b, b = _production_agent(session)
        run_id = uuid4().hex
        correlation_id = uuid4().hex
        first = create_production_scenario_run(
            session, agent_execution_intent_id=a, tenant_id=DEFAULT_TENANT_ID,
            scenario_version_id=parent_a.scope["scenario"]["id"], run_id=run_id,
            root_correlation_id=correlation_id, expires_at=parent_a.expires_at)
        session.commit()
    with Session() as request_for_b:
        with pytest.raises(Exception):
            create_production_scenario_run(
                request_for_b, agent_execution_intent_id=b, tenant_id=DEFAULT_TENANT_ID,
                scenario_version_id=parent_b.scope["scenario"]["id"], run_id=run_id,
                root_correlation_id=correlation_id, expires_at=parent_b.expires_at)
        request_for_b.rollback()
    with Session() as observer:
        stored = observer.get(ScenarioRunRow, first.id)
        assert stored.agent_execution_intent_id == a


def test_s04_legacy_scenario_run_remains_unbridged(Session):
    with Session() as session:
        run = _run(session)
        session.commit()
    with Session() as observer:
        assert observer.get(ScenarioRunRow, run.id).agent_execution_intent_id is None


@pytest.mark.parametrize("race_iteration", range(20))
def test_r2_t03_concurrent_materialization_is_singleton(Session, race_iteration):
    with Session() as setup:
        parent = _parent(setup)
        decision = _decision(setup)
        setup.commit()
        parent_id, fp = parent.id, parent.scope_fingerprint
    barrier = Barrier(2)

    def worker(_):
        with Session() as session:
            barrier.wait()
            try:
                return _materialize(session, session.get(ExecutionIntentRow, parent_id), decision, fp)
            except Exception:
                session.rollback()
                with Session() as retry:
                    return _materialize(retry, retry.get(ExecutionIntentRow, parent_id), decision, fp)

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(worker, range(2)))
    assert result[0] == result[1]
    with Session() as session:
        assert session.scalar(select(func.count()).select_from(AgentExecutionIntentRow).where(
            AgentExecutionIntentRow.execution_intent_id == parent_id)) == 1


def test_r2_d01_d08_constraints_and_legacy_nulls(Session):
    with Session() as session:
        parent = _parent(session)
        decision = _decision(session)
        agent_id = _materialize(session, parent, decision)
        with pytest.raises(Exception):
            session.execute(text("INSERT INTO agent_execution_intents (id, agent_decision_id, authorization_source, intent_type, effective_response_snapshot, status, execution_allowed, external_delivery_allowed, release_status, idempotency_key, created_at, execution_intent_id, execution_intent_fingerprint) SELECT :id, agent_decision_id, authorization_source, intent_type, effective_response_snapshot, status, execution_allowed, external_delivery_allowed, release_status, :key, created_at, execution_intent_id, execution_intent_fingerprint FROM agent_execution_intents WHERE id=:source"), {"id": f"dup-{uuid4().hex}", "key": f"dup-key-{uuid4().hex}", "source": agent_id})
        session.rollback()
        session.add(AgentExecutionIntentRow(id=f"legacy-{uuid4().hex}", agent_decision_id=decision, intent_type="WHATSAPP_RESPONSE", effective_response_snapshot="ok", idempotency_key=f"legacy-key-{uuid4().hex}", created_at=datetime.now(UTC)))
        session.flush()
        with pytest.raises(Exception):
            session.execute(text("UPDATE agent_execution_intents SET execution_intent_id=:p, execution_intent_fingerprint=:f WHERE execution_intent_id IS NULL"), {"p": parent.id, "f": parent.scope_fingerprint})
        session.rollback()
