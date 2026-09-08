from copy import deepcopy

import pytest

from attention_router.domain.execution_intent import new_execution_intent


def scope():
    return dict(
        idempotency_key="intent-key-1", scenario_version="scenario-v1",
        scenario_identity={"name": "pilot", "version": 1},
        safety_set={"name": "narrow", "version": 1}, execution_class="CONTROLLED",
        target={"kind": "synthetic_actor", "audience": "test"},
        transport={"channel": "whatsapp", "mode": "controlled"},
        budget_limits={"max_effects": 1}, operation_class="REPLY",
        immutable_inputs={"input_revision": "i1"}, policy_references=[{"id": "p1", "version": 1}],
        requested_capability="send_text", expiry_policy={"ttl_seconds": 900},
        provenance={"scenario_version": "PROVEN_FROM_CANONICAL_STATE"},
    )


def test_freeze_is_deterministic_and_inert():
    a, b = new_execution_intent(**scope()), new_execution_intent(**scope())
    assert a.freeze() == b.freeze()
    assert a.state == "FROZEN"


@pytest.mark.parametrize("field", ["scenario_version", "safety_set", "execution_class", "target", "transport", "budget_limits"])
def test_material_scope_changes_fingerprint(field):
    base = new_execution_intent(**scope())
    base.freeze()
    changed = scope()
    if isinstance(changed[field], dict):
        changed[field] = deepcopy(changed[field])
        changed[field]["changed"] = True
    else:
        changed[field] = "changed"
    other = new_execution_intent(**changed)
    other.freeze()
    assert base.fingerprint != other.fingerprint


def test_frozen_intent_cannot_be_changed_or_execute():
    intent = new_execution_intent(**scope())
    intent.freeze()
    with pytest.raises(ValueError):
        intent.update(target={"changed": True})
    assert intent.state == "FROZEN"
    assert not hasattr(intent, "readiness_result")
    assert not hasattr(intent, "scenario_run_id")


def test_retirement_does_not_authorize_or_materialize():
    intent = new_execution_intent(**scope())
    intent.freeze()
    intent.retire()
    assert intent.state == "RETIRED"
