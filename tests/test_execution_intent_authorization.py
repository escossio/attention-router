from datetime import datetime, timedelta, timezone

import pytest

from attention_router.domain.execution_intent import new_execution_intent
from attention_router.domain.human_execution_authorization import HumanExecutionAuthorization
from tests.test_execution_intent import scope


def frozen():
    intent = new_execution_intent(**scope())
    intent.freeze()
    return intent


def test_authorization_binds_frozen_intent_and_approval_has_no_execution_side_effect():
    intent = frozen()
    auth = HumanExecutionAuthorization.prepare(intent, "operator", "internal", 900, {"button": "approve"})
    auth.approve(intent, "operator")
    assert auth.state == "APPROVED"
    assert auth.execution_intent_id == intent.id
    assert auth.execution_intent_fingerprint == intent.fingerprint


def test_prepared_retired_and_changed_intents_are_rejected():
    prepared = new_execution_intent(**scope())
    with pytest.raises(ValueError):
        HumanExecutionAuthorization.prepare(prepared, "operator", "internal", 900)
    intent = frozen()
    auth = HumanExecutionAuthorization.prepare(intent, "operator", "internal", 900)
    intent.retire()
    with pytest.raises(ValueError):
        auth.approve(intent, "operator")


def test_wrong_fingerprint_sender_and_expiry_are_rejected():
    intent = frozen()
    auth = HumanExecutionAuthorization.prepare(intent, "operator", "internal", 900)
    intent2 = new_execution_intent(**{**scope(), "scenario_version": "v2"})
    intent2.freeze()
    with pytest.raises(ValueError, match="no longer authorizable"):
        auth.approve(intent2, "operator")
    auth = HumanExecutionAuthorization.prepare(intent, "operator", "internal", 900)
    auth.execution_intent_fingerprint = "wrong"
    with pytest.raises(ValueError, match="fingerprint"):
        auth.approve(intent, "operator")
    auth = HumanExecutionAuthorization.prepare(intent, "operator", "internal", 900)
    with pytest.raises(ValueError, match="approver"):
        auth.approve(intent, "other")
    auth = HumanExecutionAuthorization.prepare(intent, "operator", "internal", 1)
    with pytest.raises(ValueError, match="expired"):
        auth.approve(intent, "operator", datetime.now(timezone.utc) + timedelta(seconds=2))


def test_deny_is_single_decision():
    intent = frozen()
    auth = HumanExecutionAuthorization.prepare(intent, "operator", "internal", 900)
    auth.deny(intent, "operator")
    assert auth.state == "DENIED"
    with pytest.raises(ValueError):
        auth.approve(intent, "operator")
