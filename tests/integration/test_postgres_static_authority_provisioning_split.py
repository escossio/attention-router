"""SP01-SP18: static production authority remains recipient-independent."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import (
    AgentExecutionIntentRow, BoundedRunAuthorizationRow, EffectBudgetRow,
    ExecutionIntentRow, ExecutionIntentTargetBindingRow, ExecutionLeaseRow,
    HumanExecutionAuthorizationRow, OutboxMessageRow, PolicyRow,
    RecipientEndpointRow, ScenarioRunRow, ScenarioVersionAuthorityBindingRow,
)
from attention_router.platform.human_approval_integration import (
    build_production_human_approval_request, prepare_production_human_approval,
)
from attention_router.platform.production_authority import (
    FrozenAuthority, ProductionAuthorityDenied, canonical_recipient_address,
    production_scope_material, semantic_scope_fingerprint,
)
from attention_router.platform.production_bridge import freeze_production_execution_intent
from attention_router.platform.production_scenario_provisioning import (
    CAPABILITY, OPERATION, TRANSPORT, prepare_production_conversation_reply_target,
    provision_production_conversation_reply_authority,
)


pytestmark = pytest.mark.postgres
SNAPSHOT = "static-split-local-snapshot"


def _static(session):
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
    return provision_production_conversation_reply_authority(
        session, tenant_id=DEFAULT_TENANT_ID, policy_id=policy_id, source_sha="sp-test-source",
    )


def _counts(session):
    rows = (
        RecipientEndpointRow, ExecutionIntentTargetBindingRow, ExecutionIntentRow,
        HumanExecutionAuthorizationRow, ScenarioRunRow, AgentExecutionIntentRow,
        OutboxMessageRow, EffectBudgetRow, ExecutionLeaseRow, BoundedRunAuthorizationRow,
    )
    return tuple(session.scalar(select(func.count()).select_from(row)) for row in rows)


def _parent(session, static, address):
    now = datetime.now(UTC)
    expiry = now + timedelta(minutes=5)
    canonical = canonical_recipient_address(TRANSPORT, address)
    authority = FrozenAuthority(
        target_count=1, outbound_messages=1, action_count=1, retries=0,
        transport=TRANSPORT, operation=OPERATION, capability=CAPABILITY,
        expires_at=expiry, target_identity=(f"{DEFAULT_TENANT_ID}|{TRANSPORT}|{canonical}",),
    )
    scope = production_scope_material(
        scenario_identity={"id": static.scenario.id, "version": static.scenario.version, "content_hash": static.scenario.content_hash},
        execution_class={"id": static.execution_class.id, "identity": static.execution_class.identity, "version": static.execution_class.version},
        safety_set={"id": static.safety_set.id, "identity": static.safety_set.identity, "version": static.safety_set.version},
        policies=[{"id": static.policy.id, "policy_id": static.policy.policy_id, "version": static.policy.version, "checksum": static.policy.checksum}],
        authority_profile={"id": static.authority_profile.id, "identity": static.authority_profile.identity, "version": static.authority_profile.version},
        frozen_authority=authority, tenant_identity=DEFAULT_TENANT_ID,
        canonical_address=canonical,
        audience={"audience_type": "single_represented_owner_contact", "audience_id": "production-canary-pending"},
        immutable_inputs={"effective_response_snapshot": SNAPSHOT},
    )
    ident = f"sp-intent-{uuid4().hex}"
    parent = ExecutionIntentRow(
        id=ident, idempotency_key=ident, scope=scope,
        scope_fingerprint=semantic_scope_fingerprint(scope), provenance={"source": "sp-test"},
        state="PREPARED", created_at=now, authority_profile_id=static.authority_profile.id,
        expires_at=expiry,
    )
    session.add(parent)
    session.flush()
    return parent


def _freeze(session, parent):
    return freeze_production_execution_intent(
        session, parent=parent, expected_fingerprint=parent.scope_fingerprint,
        effective_response_snapshot=SNAPSHOT,
    )


def test_sp01_static_provision_succeeds_without_recipient(Session):
    with Session() as session:
        assert _static(session).scenario.version == 2


def test_sp02_static_creates_production_scenario_v2(Session):
    with Session() as session:
        item = _static(session).scenario
        assert item.environment_classification == "PRODUCTION" and item.version == 2


def test_sp03_static_creates_complete_authority_graph(Session):
    with Session() as session:
        static = _static(session)
        bindings = list(session.scalars(select(ScenarioVersionAuthorityBindingRow).where(
            ScenarioVersionAuthorityBindingRow.scenario_version_id == static.scenario.id
        )))
        assert {row.binding_role for row in bindings} == {"EXECUTION_CLASS", "SAFETY_SET", "POLICY", "AUTHORITY_PROFILE"}


def test_sp04_static_creates_zero_recipient_endpoints(Session):
    with Session() as session:
        before = _counts(session)[0]
        _static(session)
        assert _counts(session)[0] == before


def test_sp05_static_creates_zero_target_bindings(Session):
    with Session() as session:
        before = _counts(session)[1]
        _static(session)
        assert _counts(session)[1] == before


def test_sp06_static_creates_zero_execution_intents(Session):
    with Session() as session:
        before = _counts(session)[2]
        _static(session)
        assert _counts(session)[2] == before


def test_sp07_static_creates_zero_heas(Session):
    with Session() as session:
        before = _counts(session)[3]
        _static(session)
        assert _counts(session)[3] == before


def test_sp08_static_creates_zero_operational_artifacts(Session):
    with Session() as session:
        before = _counts(session)[4:]
        _static(session)
        assert _counts(session)[4:] == before


def test_sp09_static_replay_is_idempotent(Session):
    with Session() as session:
        first, before = _static(session), _counts(session)
        second = _static(session)
        assert first == second and _counts(session) == before


def test_sp10_synthetic_v1_is_unchanged(Session):
    with Session() as session:
        static = _static(session)
        versions = list(session.scalars(select(type(static.scenario)).where(
            type(static.scenario).scenario_definition_id == static.scenario.scenario_definition_id
        )))
        v1 = [item for item in versions if item.version == 1]
        assert not v1 or all(item.environment_classification != "PRODUCTION" for item in v1)


def test_sp11_static_graph_has_no_fixture_identity(Session):
    with Session() as session:
        static = _static(session)
        material = repr((static.scenario.manifest_source_path, static.execution_class.identity, static.safety_set.identity, static.authority_profile.identity, static.policy.config)).lower()
        assert all(value not in material for value in ("1555", "@example", "fixture", "approver"))


def test_sp12_dynamic_target_does_not_reprovision_static_authority(Session):
    with Session() as session:
        static = _static(session)
        before = (static.scenario.id, static.execution_class.id, static.safety_set.id, static.policy.id, static.authority_profile.id)
        parent = _parent(session, static, "15550000001")
        endpoint = prepare_production_conversation_reply_target(session, execution_intent_id=parent.id, tenant_id=DEFAULT_TENANT_ID, recipient_address="15550000001")
        after = _static(session)
        assert endpoint.canonical_address == "15550000001" and before == (after.scenario.id, after.execution_class.id, after.safety_set.id, after.policy.id, after.authority_profile.id)


def test_sp13_dynamic_target_is_exactly_one(Session):
    with Session() as session:
        parent = _parent(session, _static(session), "15550000001")
        endpoint = prepare_production_conversation_reply_target(session, execution_intent_id=parent.id, tenant_id=DEFAULT_TENANT_ID, recipient_address="15550000001")
        bindings = list(session.scalars(select(ExecutionIntentTargetBindingRow).where(ExecutionIntentTargetBindingRow.execution_intent_id == parent.id)))
        assert len(bindings) == 1 and bindings[0].recipient_endpoint_id == endpoint.id and endpoint.transport == TRANSPORT


def test_sp14_freeze_rejects_missing_target(Session):
    with Session() as session:
        parent = _parent(session, _static(session), "15550000001")
        with pytest.raises(ProductionAuthorityDenied, match="EXACTLY_ONE_RECIPIENT_REQUIRED"):
            _freeze(session, parent)
        assert parent.state == "PREPARED"


def test_sp15_freeze_succeeds_after_dynamic_target(Session):
    with Session() as session:
        parent = _parent(session, _static(session), "15550000001")
        prepare_production_conversation_reply_target(session, execution_intent_id=parent.id, tenant_id=DEFAULT_TENANT_ID, recipient_address="15550000001")
        assert _freeze(session, parent).state == "FROZEN"


def test_sp16_target_remains_in_semantic_fingerprint(Session):
    with Session() as session:
        static = _static(session)
        first, second = _parent(session, static, "15550000001"), _parent(session, static, "15550000002")
        prepare_production_conversation_reply_target(session, execution_intent_id=first.id, tenant_id=DEFAULT_TENANT_ID, recipient_address="15550000001")
        prepare_production_conversation_reply_target(session, execution_intent_id=second.id, tenant_id=DEFAULT_TENANT_ID, recipient_address="15550000002")
        assert _freeze(session, first).scope_fingerprint != _freeze(session, second).scope_fingerprint


def test_sp17_hea_prepares_after_dynamic_target_and_freeze(Session):
    with Session() as session:
        parent = _parent(session, _static(session), "15550000001")
        prepare_production_conversation_reply_target(session, execution_intent_id=parent.id, tenant_id=DEFAULT_TENANT_ID, recipient_address="15550000001")
        _freeze(session, parent)
        hea = prepare_production_human_approval(
            session, execution_intent_id=parent.id, expected_approver="15550009999",
            ttl_seconds=60, correlation_id=f"sp-correlation-{uuid4().hex}",
        )
        request = build_production_human_approval_request(session, authorization_id=hea.id)
        assert hea.execution_intent_fingerprint == parent.scope_fingerprint and len({button["id"] for button in request["buttons"]}) == 2


def test_sp18_static_graph_reuses_for_distinct_recipients(Session):
    with Session() as session:
        static = _static(session)
        first, second = _parent(session, static, "15550000001"), _parent(session, static, "15550000002")
        endpoint_a = prepare_production_conversation_reply_target(session, execution_intent_id=first.id, tenant_id=DEFAULT_TENANT_ID, recipient_address="15550000001")
        endpoint_b = prepare_production_conversation_reply_target(session, execution_intent_id=second.id, tenant_id=DEFAULT_TENANT_ID, recipient_address="15550000002")
        _freeze(session, first)
        _freeze(session, second)
        assert endpoint_a.id != endpoint_b.id and first.scope_fingerprint != second.scope_fingerprint and _static(session).scenario.id == static.scenario.id
