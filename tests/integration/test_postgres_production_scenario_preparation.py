"""PS01-PS16: production-only conversation.reply authority preparation."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import (
    AgentExecutionIntentRow, ExecutionIntentRow, ExecutionIntentTargetBindingRow,
    OutboxMessageRow, PolicyRow, ScenarioRunRow,
)
from attention_router.platform.human_approval_integration import (
    build_production_human_approval_request, prepare_production_human_approval,
)
from attention_router.platform.production_authority import (
    FrozenAuthority, production_scope_material, semantic_scope_fingerprint,
)
from attention_router.platform.production_bridge import validate_frozen_execution_intent_authority
from attention_router.platform.production_scenario_provisioning import (
    CAPABILITY, OPERATION, TRANSPORT, provision_production_conversation_reply,
)


pytestmark = pytest.mark.postgres


def _graph(session):
    policy_id = "production_reply_policy"
    if session.get(PolicyRow, policy_id) is None:
        session.add(PolicyRow(
            identifier=policy_id, tenant_id=DEFAULT_TENANT_ID,
            name="Production conversation reply", match_criteria={}, priority=0,
            specificity=0, tone="neutral", initial_wait_seconds=0,
            allowed_disclosures=[], allowed_actions=[OPERATION], escalation_steps=[],
            ack_timeout_seconds=0, repetition_limit=1, cancellation_conditions=[],
            completion_conditions=[], current_version_id=None, is_active=True,
        ))
        session.flush()
    return provision_production_conversation_reply(
        session, tenant_id=DEFAULT_TENANT_ID, policy_id=policy_id,
        recipient_address="1 (555) 000-0001", source_sha="ps-test-source",
    )


def _frozen_intent(session, graph):
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=5)
    authority = FrozenAuthority(
        target_count=1, outbound_messages=1, action_count=1, retries=0,
        transport=TRANSPORT, operation=OPERATION, capability=CAPABILITY,
        expires_at=expires,
        target_identity=(f"{DEFAULT_TENANT_ID}|{TRANSPORT}|{graph.recipient_endpoint.canonical_address}",),
    )
    scope = production_scope_material(
        scenario_identity={"id": graph.scenario.id, "version": graph.scenario.version, "content_hash": graph.scenario.content_hash},
        execution_class={"id": graph.execution_class.id, "identity": graph.execution_class.identity, "version": graph.execution_class.version},
        safety_set={"id": graph.safety_set.id, "identity": graph.safety_set.identity, "version": graph.safety_set.version},
        policies=[{"id": graph.policy.id, "policy_id": graph.policy.policy_id, "version": graph.policy.version, "checksum": graph.policy.checksum}],
        authority_profile={"id": graph.authority_profile.id, "identity": graph.authority_profile.identity, "version": graph.authority_profile.version},
        frozen_authority=authority, tenant_identity=DEFAULT_TENANT_ID,
        canonical_address=graph.recipient_endpoint.canonical_address,
        audience={"audience_type": "single_represented_owner_contact", "audience_id": "production-canary-pending"},
        immutable_inputs={"effective_response_snapshot": "production-canary-placeholder"},
    )
    ident = f"ps-intent-{uuid4().hex}"
    parent = ExecutionIntentRow(
        id=ident, idempotency_key=ident, scope=scope,
        scope_fingerprint=semantic_scope_fingerprint(scope), provenance={"source": "ps-test"},
        state="PREPARED", created_at=now, authority_profile_id=graph.authority_profile.id,
        expires_at=expires,
    )
    session.add(parent)
    session.flush()
    session.add(ExecutionIntentTargetBindingRow(
        id=f"ps-target-{uuid4().hex}", execution_intent_id=parent.id,
        target_type="WHATSAPP_RECIPIENT_ENDPOINT", recipient_endpoint_id=graph.recipient_endpoint.id,
        ordinal=0, created_at=now,
    ))
    session.flush()
    parent.state, parent.frozen_at = "FROZEN", now
    session.flush()
    return parent


def test_ps01_production_execution_class_valid(Session):
    with Session() as session:
        item = _graph(session).execution_class
        assert item.environment_classification == "PRODUCTION" and item.allowed_capabilities == [CAPABILITY]


def test_ps02_production_safety_set_valid(Session):
    with Session() as session:
        item = _graph(session).safety_set
        assert item.environment_classification == "PRODUCTION" and item.max_outbound_messages == 1


def test_ps03_production_policy_valid(Session):
    with Session() as session:
        item = _graph(session).policy
        assert item.environment_classification == "PRODUCTION" and item.status == "ACTIVE"


def test_ps04_authority_profile_complete(Session):
    with Session() as session:
        item = _graph(session).authority_profile
        assert (item.allowed_transport, item.allowed_operation, item.allowed_capability, item.max_retries) == (TRANSPORT, OPERATION, CAPABILITY, 0)


def test_ps05_production_scenario_version_valid(Session):
    with Session() as session:
        item = _graph(session).scenario
        assert item.version == 2 and item.environment_classification == "PRODUCTION" and item.enabled


def test_ps06_scenario_authority_binding_valid(Session):
    with Session() as session:
        graph = _graph(session)
        parent = _frozen_intent(session, graph)
        validate_frozen_execution_intent_authority(session, parent=parent, expected_fingerprint=parent.scope_fingerprint, effective_response_snapshot="production-canary-placeholder")


def test_ps07_recipient_endpoint_semantics_valid(Session):
    with Session() as session:
        item = _graph(session).recipient_endpoint
        assert item.transport == TRANSPORT and item.canonical_address == "15550000001" and item.status == "ACTIVE"


def test_ps08_exact_one_target_binding_valid(Session):
    with Session() as session:
        parent = _frozen_intent(session, _graph(session))
        assert session.scalar(select(func.count()).select_from(ExecutionIntentTargetBindingRow).where(ExecutionIntentTargetBindingRow.execution_intent_id == parent.id)) == 1


def test_ps09_audience_explicit(Session):
    with Session() as session:
        parent = _frozen_intent(session, _graph(session))
        assert parent.scope["audience"]["audience_type"] == "single_represented_owner_contact"


def test_ps10_transport_and_capability_exact(Session):
    with Session() as session:
        parent = _frozen_intent(session, _graph(session))
        frozen = parent.scope["frozen_authority"]
        assert (frozen["transport"], frozen["operation"], frozen["capability"]) == (TRANSPORT, OPERATION, CAPABILITY)


def test_ps11_limits_finite_and_valid(Session):
    with Session() as session:
        parent = _frozen_intent(session, _graph(session))
        frozen = parent.scope["frozen_authority"]
        assert [frozen[key] for key in ("target_count", "outbound_messages", "action_count", "retries")] == [1, 1, 1, 0]


def test_ps12_immutable_input_contract_valid(Session):
    with Session() as session:
        parent = _frozen_intent(session, _graph(session))
        assert semantic_scope_fingerprint(parent.scope) == parent.scope_fingerprint
        assert parent.scope["immutable_inputs"]["effective_response_snapshot"]


def test_ps13_expiry_contract_valid(Session):
    with Session() as session:
        parent = _frozen_intent(session, _graph(session))
        with pytest.raises(ValueError, match="EXECUTION_INTENT_EXPIRED"):
            validate_frozen_execution_intent_authority(session, parent=parent, expected_fingerprint=parent.scope_fingerprint, effective_response_snapshot="production-canary-placeholder", now=parent.expires_at)


def test_ps14_full_authority_graph_fingerprint_stable(Session):
    with Session() as session:
        parent = _frozen_intent(session, _graph(session))
        assert semantic_scope_fingerprint(parent.scope) == semantic_scope_fingerprint(dict(parent.scope))


def test_ps15_full_graph_creates_frozen_intent_without_operational_artifacts(Session):
    with Session() as session:
        parent = _frozen_intent(session, _graph(session))
        validate_frozen_execution_intent_authority(session, parent=parent, expected_fingerprint=parent.scope_fingerprint, effective_response_snapshot="production-canary-placeholder")
        assert parent.state == "FROZEN"
        for table in (AgentExecutionIntentRow, ScenarioRunRow, OutboxMessageRow):
            assert session.scalar(select(func.count()).select_from(table)) == 0


def test_ps16_full_graph_prepares_local_hea_without_outbound(Session):
    with Session() as session:
        parent = _frozen_intent(session, _graph(session))
        hea = prepare_production_human_approval(session, execution_intent_id=parent.id, expected_approver="15550000001", ttl_seconds=60, correlation_id=uuid4().hex)
        request = build_production_human_approval_request(session, authorization_id=hea.id)
        assert hea.execution_intent_fingerprint == parent.scope_fingerprint
        assert {button["reply"]["title"] for button in request["payload"]["interactive"]["action"]["buttons"]} == {"Aprovar", "Negar"}
        assert session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 0
