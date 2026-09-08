from attention_router.application.platform.entities import create_relationship
from attention_router.application.platform.pre_run_context import resolve_pre_run_execution_context
from attention_router.core.entities import EntityReference
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.repository import upsert_actor_binding
from attention_router.platform.execution_contract import ContractState, get_execution_runtime_contract


def _actors(session):
    upsert_actor_binding(session, "test", "synthetic", "synthetic-key", "SYNTHETIC_TEST_ACTOR", metadata={"audience": "synthetic_test"})
    upsert_actor_binding(session, "test", "owner", "owner-key", "owner", metadata={"owner": True})
    session.flush()


def test_pre_run_context_resolves_canonical_actor_owner_relationship_and_audience(session):
    _actors(session)
    create_relationship(
        session, tenant_id=DEFAULT_TENANT_ID,
        source=EntityReference(entity_type="ACTOR", entity_id="synthetic-key"),
        target=EntityReference(entity_type="ACTOR", entity_id="owner-key"),
        relationship_type="synthetic_test",
    )
    context = resolve_pre_run_execution_context(
        session, tenant_id=DEFAULT_TENANT_ID, scenario_id="generic", scenario_version=1,
        synthetic_actor_key="synthetic-key",
    )
    assert context.synthetic_actor_ready
    assert context.synthetic_actor_not_owner
    assert context.owner_target_resolved
    assert context.relationship.state == "READY"
    assert context.audience.audience == "synthetic_test"


def test_pre_run_context_fails_closed_for_missing_actor_or_owner(session):
    context = resolve_pre_run_execution_context(
        session, tenant_id=DEFAULT_TENANT_ID, scenario_id="generic", scenario_version=1,
        synthetic_actor_key="missing",
    )
    assert not context.synthetic_actor_ready
    assert not context.owner_target_resolved
    assert context.relationship.state == "UNKNOWN"


def test_execution_runtime_contract_is_structural_and_read_only():
    contract = get_execution_runtime_contract()
    assert contract.supported
    assert contract.lease.state is ContractState.SUPPORTED
    assert contract.outbox_idempotency.state is ContractState.SUPPORTED
    assert contract.correlation_propagation.state is ContractState.SUPPORTED
