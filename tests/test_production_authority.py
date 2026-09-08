from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from attention_router.platform.production_authority import (
    ProductionAuthorityDenied,
    FrozenAuthority,
    authority_is_subset,
    validate_not_expired,
    validate_single_recipient,
    validate_production_scope_dependency_graph,
    canonical_recipient_address,
    production_scope_fingerprint,
)


def component(**values):
    return SimpleNamespace(status="ACTIVE", environment_classification="PRODUCTION", **values)


def test_production_graph_requires_structural_production_components():
    args = dict(
        scenario_version=component(), execution_class=component(
            allowed_transports=["meta_whatsapp"], allowed_operations=["conversation.reply"],
            allowed_capabilities=["conversation.reply"]),
        safety_set=component(allowed_transport="meta_whatsapp", allowed_operation="conversation.reply",
                             allowed_capability="conversation.reply"),
        policy=component(), authority_profile=component(), transport="meta_whatsapp",
        operation="conversation.reply", capability="conversation.reply",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    validate_production_scope_dependency_graph(**args)
    args["policy"].environment_classification = "SYNTHETIC"
    with pytest.raises(ProductionAuthorityDenied):
        validate_production_scope_dependency_graph(**args)


def test_single_recipient_and_subset_are_fail_closed():
    validate_single_recipient(target_type="WHATSAPP_RECIPIENT_ENDPOINT", target_count=1,
                              endpoint_transport="meta_whatsapp", transport="meta_whatsapp")
    with pytest.raises(ProductionAuthorityDenied):
        validate_single_recipient(target_type="WHATSAPP_RECIPIENT_ENDPOINT", target_count=2,
                                  endpoint_transport="meta_whatsapp", transport="meta_whatsapp")
    expires = datetime.now(UTC) + timedelta(seconds=60)
    parent = FrozenAuthority(1, 1, 1, 0, "meta_whatsapp", "conversation.reply", "conversation.reply", expires)
    child = FrozenAuthority(1, 1, 1, 0, "meta_whatsapp", "conversation.reply", "conversation.reply", expires)
    assert authority_is_subset(child, parent)
    assert not authority_is_subset(FrozenAuthority(2, 1, 1, 0, parent.transport, parent.operation,
                                                    parent.capability, parent.expires_at), parent)


def test_expiry_never_authorizes_execution():
    with pytest.raises(ProductionAuthorityDenied, match="EXPIRED"):
        validate_not_expired(datetime.now(UTC) - timedelta(seconds=1))


def test_recipient_normalization_is_semantic_and_deterministic():
    assert canonical_recipient_address("meta_whatsapp", "+55 (00) 00000-0034") == "5500000000034"
    assert canonical_recipient_address("meta_whatsapp", "5500000000034") == "5500000000034"
    with pytest.raises(ProductionAuthorityDenied):
        canonical_recipient_address("meta_whatsapp", "---")


def test_production_fingerprint_changes_for_target_budget_policy_and_expiry():
    expiry = datetime.now(UTC) + timedelta(minutes=5)
    authority = FrozenAuthority(1, 1, 1, 0, "meta_whatsapp", "conversation.reply", "conversation.reply", expiry, ("tenant|meta_whatsapp|1",))

    def make(**changes):
        return production_scope_fingerprint(
            scenario_identity={"id": "scenario", "version": 2}, execution_class={"id": "class", "version": 1},
            safety_set={"id": "safety", "version": 1}, policies=[{"id": changes.get("policy", "policy"), "version": 1}],
            authority_profile={"id": "profile", "version": 1},
            frozen_authority=changes.get("authority", authority), tenant_identity="tenant",
            canonical_address=changes.get("address", "1"), audience={"audience_type": "single_represented_owner_contact"}, immutable_inputs={},
        )

    baseline = make()
    assert baseline != make(address="2")
    assert baseline != make(policy="policy-2")
    assert baseline != make(authority=FrozenAuthority(1, 1, 2, 0, authority.transport, authority.operation, authority.capability, expiry, authority.target_identity))
    assert baseline != make(authority=FrozenAuthority(1, 1, 1, 0, authority.transport, authority.operation, authority.capability, expiry + timedelta(seconds=1), authority.target_identity))
