"""PostgreSQL adversarial proof for frozen production authority semantics."""

from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from attention_router.infrastructure.models import (
    AgentExecutionIntentRow,
    ExecutionIntentTargetBindingRow,
    HumanExecutionAuthorizationRow,
    ScenarioRunRow,
)
from attention_router.platform.production_authority import (
    ProductionAuthorityDenied,
    frozen_authority_from_scope,
    semantic_scope_fingerprint,
)
from attention_router.platform.production_bridge import (
    create_production_scenario_run,
    materialize_production_agent_intent,
)
from tests.integration.test_postgres_10_5c_6i1a_r2_bridge_adversarial import (
    _decision,
    _parent,
)


pytestmark = pytest.mark.postgres


def _materialize(session, parent, *, fingerprint=None, response="ok", decision_id=None, now=None):
    return materialize_production_agent_intent(
        session,
        execution_intent_id=parent.id,
        expected_fingerprint=fingerprint or parent.scope_fingerprint,
        agent_decision_id=decision_id or _decision(session),
        effective_response_snapshot=response,
        now=now,
    )


def _changed_fingerprint(parent, change) -> str:
    changed = deepcopy(parent.scope)
    change(changed)
    return semantic_scope_fingerprint(changed)


def _assert_changed_scope_rejected(session, parent, change) -> None:
    with pytest.raises(
        ProductionAuthorityDenied,
        match="EXECUTION_INTENT_FINGERPRINT_MISMATCH",
    ):
        _materialize(session, parent, fingerprint=_changed_fingerprint(parent, change))


def test_sa01_exact_projection_succeeds(Session):
    with Session() as session:
        parent = _parent(session)
        projection = _materialize(session, parent)
        assert projection.execution_intent_id == parent.id
        assert projection.execution_intent_fingerprint == parent.scope_fingerprint
        assert projection.recipient_reference == parent.scope["target"]["canonical_address"]
        assert projection.capability_name == parent.scope["frozen_authority"]["capability"]
        assert projection.capability_request["scope_fingerprint"] == parent.scope_fingerprint
        assert projection.capability_request["audience"] == parent.scope["audience"]
        session.commit()
        with pytest.raises(DBAPIError, match="production agent intent bridge is write-once"):
            session.execute(
                text("UPDATE agent_execution_intents SET capability_name='admin.execute' WHERE id=:id"),
                {"id": projection.id},
            )


def test_sa02_target_widening_rejected(Session):
    with Session() as session:
        parent = _parent(session)
        for change in (
            lambda scope: scope["target"].update(canonical_address="5500000000037"),
            lambda scope: scope["frozen_authority"].update(target_count=2),
            lambda scope: scope["frozen_authority"]["target_identity"].append(
                "tenant|meta_whatsapp|second-target"
            ),
        ):
            _assert_changed_scope_rejected(session, parent, change)
        session.commit()
        target = session.scalar(select(ExecutionIntentTargetBindingRow).where(
            ExecutionIntentTargetBindingRow.execution_intent_id == parent.id
        ))
        with pytest.raises(DBAPIError):
            session.execute(
                text("UPDATE recipient_endpoints SET canonical_address='5500000000037' WHERE id=:id"),
                {"id": target.recipient_endpoint_id},
            )
        session.rollback()
        with pytest.raises(DBAPIError, match="execution intent target bindings are write-once"):
            session.execute(
                text("UPDATE execution_intent_target_bindings SET ordinal=1 WHERE id=:id"),
                {"id": target.id},
            )


def test_sa03_audience_widening_rejected(Session):
    with Session() as session:
        parent = _parent(session)
        _assert_changed_scope_rejected(
            session,
            parent,
            lambda scope: scope.update(audience={"audience_type": "everyone"}),
        )


def test_sa04_transport_channel_mutation_rejected(Session):
    with Session() as session:
        parent = _parent(session)
        _assert_changed_scope_rejected(
            session,
            parent,
            lambda scope: scope["frozen_authority"].update(transport="email"),
        )


def test_sa05_operation_capability_mutation_rejected(Session):
    with Session() as session:
        parent = _parent(session)
        _assert_changed_scope_rejected(
            session,
            parent,
            lambda scope: scope["frozen_authority"].update(operation="conversation.delete"),
        )
        _assert_changed_scope_rejected(
            session,
            parent,
            lambda scope: scope["frozen_authority"].update(capability="admin.execute"),
        )


def test_sa06_budget_limit_widening_rejected(Session):
    with Session() as session:
        parent = _parent(session)
        for field in ("target_count", "outbound_messages", "action_count", "retries"):
            _assert_changed_scope_rejected(
                session,
                parent,
                lambda scope, field=field: scope["frozen_authority"].update({field: 2}),
            )
        session.commit()
        profile_id = parent.scope["authority_profile"]["id"]
        with pytest.raises(DBAPIError):
            session.execute(
                text(
                    "UPDATE static_intent_authority_profiles "
                    "SET max_outbound_messages=2 WHERE id=:id"
                ),
                {"id": profile_id},
            )


def test_sa07_execution_class_mutation_rejected(Session):
    with Session() as session:
        parent = _parent(session)
        _assert_changed_scope_rejected(
            session,
            parent,
            lambda scope: scope["execution_class"].update(version=2),
        )
        session.commit()
        class_id = parent.scope["execution_class"]["id"]
        with pytest.raises(DBAPIError):
            session.execute(
                text("UPDATE execution_class_versions SET allowed_operations='[]' WHERE id=:id"),
                {"id": class_id},
            )
        session.rollback()
        scenario_id = parent.scope["scenario"]["id"]
        with pytest.raises(DBAPIError):
            session.execute(
                text("UPDATE scenario_versions SET content_hash='changed' WHERE id=:id"),
                {"id": scenario_id},
            )


def test_sa08_safety_set_mutation_rejected(Session):
    with Session() as session:
        parent = _parent(session)
        _assert_changed_scope_rejected(
            session,
            parent,
            lambda scope: scope["safety_set"].update(version=2),
        )
        session.commit()
        safety_id = parent.scope["safety_set"]["id"]
        with pytest.raises(DBAPIError):
            session.execute(
                text("UPDATE safety_set_versions SET required_invariants='[]' WHERE id=:id"),
                {"id": safety_id},
            )
        session.rollback()
        with pytest.raises(DBAPIError, match="scenario authority bindings are append-only"):
            session.execute(
                text(
                    "DELETE FROM scenario_version_authority_bindings "
                    "WHERE scenario_version_id=:id AND binding_role='SAFETY_SET'"
                ),
                {"id": parent.scope["scenario"]["id"]},
            )


def test_sa09_policy_version_mutation_and_toctou_rejected(Session):
    with Session() as session:
        parent = _parent(session)
        decision_id = _decision(session)
        session.commit()
        policy_id = parent.scope["policies"][0]["id"]
        session.execute(
            text("UPDATE policy_versions SET status='RETIRED' WHERE id=:id"),
            {"id": policy_id},
        )
        with pytest.raises(ProductionAuthorityDenied, match="PRODUCTION_DEPENDENCY_INACTIVE"):
            _materialize(session, parent, decision_id=decision_id)


def test_sa10_immutable_input_mutation_rejected(Session):
    with Session() as session:
        parent = _parent(session)
        decision_id = _decision(session)
        session.commit()
        with pytest.raises(DBAPIError, match="frozen execution authority is immutable"):
            session.execute(
                text(
                    "UPDATE execution_intents SET scope = jsonb_set("
                    "scope, '{immutable_inputs,effective_response_snapshot}', '"
                    '"changed"'
                    "') WHERE id = :intent_id"
                ),
                {"intent_id": parent.id},
            )
        session.rollback()
        parent = session.get(type(parent), parent.id)
        with pytest.raises(ProductionAuthorityDenied, match="IMMUTABLE_INPUT_MISMATCH"):
            _materialize(session, parent, response="changed", decision_id=decision_id)


def test_sa11_expiry_widening_and_revival_rejected(Session):
    with Session() as session:
        parent = _parent(session)
        decision_id = _decision(session)
        session.commit()
        with pytest.raises(ProductionAuthorityDenied, match="EXECUTION_INTENT_EXPIRED"):
            _materialize(session, parent, decision_id=decision_id, now=parent.expires_at)
        with pytest.raises(DBAPIError, match="frozen execution authority is immutable"):
            session.execute(
                text("UPDATE execution_intents SET expires_at=expires_at + interval '1 hour' WHERE id=:id"),
                {"id": parent.id},
            )
        session.rollback()
        parent = session.get(type(parent), parent.id)
        projection = _materialize(
            session,
            parent,
            decision_id=decision_id,
            now=parent.expires_at - timedelta(seconds=1),
        )
        with pytest.raises(ProductionAuthorityDenied, match="PRODUCTION_RUN_EXPIRY_WIDENING"):
            create_production_scenario_run(
                session,
                agent_execution_intent_id=projection.id,
                tenant_id=parent.scope["target"]["tenant"],
                scenario_version_id=parent.scope["scenario"]["id"],
                run_id="sa11-run",
                root_correlation_id="sa11-correlation",
                expires_at=parent.expires_at + timedelta(seconds=1),
            )


def test_sa12_fingerprint_mismatch_rejected(Session):
    with Session() as session:
        parent = _parent(session)
        with pytest.raises(
            ProductionAuthorityDenied,
            match="EXECUTION_INTENT_FINGERPRINT_MISMATCH",
        ):
            _materialize(session, parent, fingerprint="not-the-frozen-fingerprint")
        projection = _materialize(session, parent)
        assert projection.execution_intent_fingerprint == parent.scope_fingerprint


def test_sa13_technical_metadata_does_not_change_fingerprint(Session):
    with Session() as session:
        parent = _parent(session)
        baseline = parent.scope_fingerprint
        approved = session.scalar(select(HumanExecutionAuthorizationRow).where(
            HumanExecutionAuthorizationRow.execution_intent_id == parent.id
        ))
        parent.provenance = {"source": "other-technical-source", "trace": "sa13"}
        approved.request_wamid = "wamid.technical"
        approved.correlation_id = "sa13-technical-correlation"
        session.flush()
        assert semantic_scope_fingerprint(parent.scope) == baseline
        assert _materialize(session, parent).execution_intent_fingerprint == baseline


def test_sa14_hea_metadata_cannot_widen_authority(Session):
    with Session() as session:
        before_agents = session.scalar(select(func.count()).select_from(AgentExecutionIntentRow))
        before_runs = session.scalar(select(func.count()).select_from(ScenarioRunRow))
        parent = _parent(session)
        approved = session.scalar(select(HumanExecutionAuthorizationRow).where(
            HumanExecutionAuthorizationRow.execution_intent_id == parent.id
        ))
        approved.scope = {
            "target": "everyone",
            "capability": "admin.execute",
            "budget_limits": {"max_outbound_messages": 999},
        }
        approved.request_wamid = "wamid.hea-metadata"
        session.flush()
        assert session.scalar(select(func.count()).select_from(AgentExecutionIntentRow)) == before_agents
        assert session.scalar(select(func.count()).select_from(ScenarioRunRow)) == before_runs
        projection = _materialize(session, parent)
        assert projection.recipient_reference == parent.scope["target"]["canonical_address"]
        assert projection.capability_name == "conversation.reply"
        assert "admin.execute" not in str(projection.capability_request)


def test_sa15_projection_narrowing_is_rejected_by_exact_relation(Session):
    with Session() as session:
        parent = _parent(session)
        _assert_changed_scope_rejected(
            session,
            parent,
            lambda scope: scope["frozen_authority"].update(outbound_messages=0),
        )
        assert _materialize(session, parent).execution_intent_id == parent.id


def test_sa16_absent_parent_authority_cannot_be_injected(Session):
    with Session() as session:
        parent = _parent(session)
        changed = deepcopy(parent.scope)
        changed["new_semantic_authority"] = {"capability": "admin.execute"}
        with pytest.raises(ProductionAuthorityDenied, match="SEMANTIC_SCOPE_SCHEMA_MISMATCH"):
            frozen_authority_from_scope(changed)
        with pytest.raises(
            ProductionAuthorityDenied,
            match="EXECUTION_INTENT_FINGERPRINT_MISMATCH",
        ):
            _materialize(session, parent, fingerprint=semantic_scope_fingerprint(changed))


def test_sa17_cross_target_replay_cannot_reuse_projection(Session):
    with Session() as session:
        parent_a = _parent(session)
        parent_b = _parent(session)
        decision_id = _decision(session)
        projection_a = _materialize(session, parent_a, decision_id=decision_id)
        with pytest.raises(
            ProductionAuthorityDenied,
            match="EXECUTION_INTENT_FINGERPRINT_MISMATCH",
        ):
            _materialize(
                session,
                parent_b,
                fingerprint=parent_a.scope_fingerprint,
                decision_id=decision_id,
            )
        projection_b = _materialize(session, parent_b, decision_id=decision_id)
        assert projection_b.id != projection_a.id
        assert projection_b.recipient_reference != projection_a.recipient_reference


def test_sa18_same_frozen_semantics_replay_is_idempotent(Session):
    with Session() as session:
        parent = _parent(session)
        decision_id = _decision(session)
        first = _materialize(session, parent, decision_id=decision_id)
        second = _materialize(session, parent, decision_id=decision_id)
        assert second.id == first.id
        assert session.scalar(select(func.count()).select_from(AgentExecutionIntentRow).where(
            AgentExecutionIntentRow.execution_intent_id == parent.id
        )) == 1
