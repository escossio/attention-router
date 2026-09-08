from datetime import UTC, datetime, timedelta

import pytest

from attention_router.platform.bounded_authorization import (
    BoundedAuthorizationDenied,
    check_bounded_authorization,
    create_bounded_authorization,
    revoke_bounded_authorization,
)
from attention_router.infrastructure.models import EffectConsumptionRow


def _values():
    now = datetime.now(UTC)
    return dict(
        tenant_id="00000000-0000-4000-8000-000000000001", scenario_run_id="run-1", effect_budget_id="budget-1",
        level="L1_SYNTHETIC_E2E", actor_scope="actor-synthetic",
        target_scope="target-synthetic", capability_scope="conversation.reply",
        effect_scope="effect-1", max_effects=1, authorized_by="HUMAN_OPERATOR",
        correlation_id="corr-1", valid_from=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=5), now=now,
    )


def test_bounded_authorization_is_scoped_and_revocable(session):
    row = create_bounded_authorization(session, **_values())
    session.commit()
    values = _values()
    values.pop("max_effects")
    values.pop("authorized_by")
    values.pop("correlation_id")
    values.pop("valid_from")
    values.pop("expires_at")
    assert check_bounded_authorization(session, **values).allowed is True
    values["target_scope"] = "other-target"
    assert check_bounded_authorization(session, **values).reason_code == "TARGET_SCOPE_MISMATCH"
    revoke_bounded_authorization(session, row.id)
    session.commit()
    values["target_scope"] = "target-synthetic"
    assert check_bounded_authorization(session, **values).reason_code == "AUTHORIZATION_REVOKED"


def test_missing_expired_and_forbidden_authority_fail_closed(session):
    values = _values()
    values.pop("max_effects")
    values.pop("authorized_by")
    values.pop("correlation_id")
    values.pop("valid_from")
    values.pop("expires_at")
    assert check_bounded_authorization(session, **values).reason_code == "AUTHORIZATION_MISSING"
    expired = _values()
    expired["valid_from"] = expired["now"] - timedelta(hours=2)
    expired["expires_at"] = expired["now"] - timedelta(hours=1)
    create_bounded_authorization(session, **expired)
    session.commit()
    values["effect_budget_id"] = expired["effect_budget_id"]
    assert check_bounded_authorization(session, **values).reason_code == "AUTHORIZATION_EXPIRED"
    forbidden = _values()
    forbidden["effect_budget_id"] = "budget-2"
    forbidden["authorized_by"] = "ANDY"
    with pytest.raises(BoundedAuthorizationDenied, match="AUTHORITY_SOURCE_FORBIDDEN"):
        create_bounded_authorization(session, **forbidden)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("scenario_run_id", "other-run", "AUTHORIZATION_MISSING"),
        ("tenant_id", "other-tenant", "AUTHORIZATION_MISSING"),
        ("level", "L0_UNIT_DRY", "LEVEL_SCOPE_MISMATCH"),
        ("actor_scope", "other-actor", "ACTOR_SCOPE_MISMATCH"),
        ("target_scope", "other-target", "TARGET_SCOPE_MISMATCH"),
        ("capability_scope", "calendar.write", "CAPABILITY_SCOPE_MISMATCH"),
        ("effect_scope", "other-effect", "EFFECT_SCOPE_MISMATCH"),
    ],
)
def test_scope_matrix_is_fail_closed(session, field, value, reason):
    create_bounded_authorization(session, **_values())
    session.commit()
    values = _values()
    for key in ("max_effects", "authorized_by", "correlation_id", "valid_from", "expires_at"):
        values.pop(key)
    values[field] = value
    result = check_bounded_authorization(session, **values)
    assert result.allowed is False
    assert result.reason_code == reason


def test_budget_exhaustion_is_bound_to_logical_effect(session):
    values = _values()
    row = create_bounded_authorization(session, **values)
    session.add(
        EffectConsumptionRow(
            id="consumed-1", tenant_id=values["tenant_id"], effect_budget_id=values["effect_budget_id"],
            logical_effect_id=values["effect_scope"], direction="SYSTEM", target_scope=values["target_scope"],
            execution_lease_id="lease-1", idempotency_key="effect-1", state="CONSUMED",
            reserved_at=values["now"], consumed_at=values["now"], provenance={}, created_at=values["now"],
        )
    )
    session.commit()
    check_values = {key: values[key] for key in (
        "tenant_id", "scenario_run_id", "effect_budget_id", "level", "actor_scope",
        "target_scope", "capability_scope", "effect_scope", "now",
    )}
    result = check_bounded_authorization(session, **check_values)
    assert result.allowed is False
    assert result.reason_code == "EFFECT_BUDGET_EXHAUSTED"
    assert result.authorization_id == row.id
