from datetime import UTC, datetime, timedelta
from itertools import permutations

import pytest

from attention_router.platform.meta_callback_reconciliation import (
    ADMISSION_GRACE_SECONDS,
    MAX_ADMISSION_TRANSACTION_SECONDS,
    META_DELIVERY_REASON,
    META_FAILED_REASON,
    META_INVALID_REASON,
    META_RECONCILIATION_GLOBAL_LOCK_ORDER,
    META_TIMEOUT_REASON,
    RECONCILIATION_WINDOW_SECONDS,
    MetaCallbackEvidence,
    admit_meta_callback_evidence,
    parse_provider_timestamp,
    reduce_meta_callback_evidence,
)


ACCEPTED = datetime(2026, 9, 1, tzinfo=UTC)
DEADLINE = ACCEPTED + timedelta(seconds=RECONCILIATION_WINDOW_SECONDS)
CLOSURE = DEADLINE + timedelta(seconds=ADMISSION_GRACE_SECONDS)
T1 = 1_788_220_800
T2 = T1 + 60


def _evidence(
    status,
    timestamp,
    *,
    received_at=ACCEPTED + timedelta(minutes=1),
    errors_present=False,
):
    return MetaCallbackEvidence(
        status=status,
        provider_timestamp_raw=timestamp,
        received_at=received_at,
        errors_present=errors_present,
    )


def _reduce(evidences, *, now=CLOSURE):
    return reduce_meta_callback_evidence(
        evidences,
        accepted_at=ACCEPTED,
        now=now,
    )


def test_frozen_window_grace_and_admission_timeout_are_monotonic():
    assert RECONCILIATION_WINDOW_SECONDS == 604800
    assert ADMISSION_GRACE_SECONDS == 30
    assert 0 < MAX_ADMISSION_TRANSACTION_SECONDS < ADMISSION_GRACE_SECONDS


def test_admission_timeout_must_be_strictly_inside_configured_grace():
    with pytest.raises(ValueError, match="inside grace"):
        admit_meta_callback_evidence(
            lambda: None,
            status_event={},
            received_at=ACCEPTED,
            max_transaction_seconds=30,
            admission_grace_seconds=30,
        )


def test_global_lock_order_is_single_and_explicit():
    assert META_RECONCILIATION_GLOBAL_LOCK_ORDER == (
        "meta_delivery_reconciliation",
        "outbox",
        "execution_lease",
        "effect_budget",
        "effect_consumption",
        "agent_execution_intent",
        "scenario_run",
        "bounded_run_authorization",
        "human_execution_authorization",
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (_evidence("failed", T1), _evidence("delivered", T2)),
        (_evidence("delivered", T2), _evidence("failed", T1)),
        (_evidence("delivered", T1), _evidence("failed", T2)),
        (_evidence("failed", T2), _evidence("delivered", T1)),
    ],
)
def test_delivery_dominates_failure_independent_of_time_and_arrival(first, second):
    for evidence_order in permutations((first, second)):
        result = _reduce(evidence_order, now=ACCEPTED + timedelta(hours=1))
        assert result.decision == "PASSED"
        assert result.reason_code == META_DELIVERY_REASON
        assert result.delivery_proved is True
        assert result.evidence_class == "DELIVERY_PROVED_WITH_FAILED_CONFLICT"


def test_read_implies_delivery_and_sent_does_not():
    read = _reduce([_evidence("read", T1)], now=ACCEPTED + timedelta(minutes=2))
    sent = _reduce([_evidence("sent", T1)], now=CLOSURE - timedelta(microseconds=1))
    assert (read.decision, read.delivery_proved) == ("PASSED", True)
    assert (sent.decision, sent.delivery_proved) == ("PENDING", False)


@pytest.mark.parametrize(
    ("evidences", "reason"),
    [
        ([_evidence("sent", T1), _evidence("failed", T2)], META_FAILED_REASON),
        ([_evidence("failed", T1), _evidence("sent", T2)], META_TIMEOUT_REASON),
        ([_evidence("sent", T1)], META_TIMEOUT_REASON),
        ([_evidence("failed", T1)], META_FAILED_REASON),
        ([], META_TIMEOUT_REASON),
    ],
)
def test_negative_result_waits_for_closure_barrier(evidences, reason):
    before_deadline = _reduce(evidences, now=DEADLINE - timedelta(microseconds=1))
    at_deadline = _reduce(evidences, now=DEADLINE)
    before_closure = _reduce(evidences, now=CLOSURE - timedelta(microseconds=1))
    at_closure = _reduce(evidences, now=CLOSURE)
    assert before_deadline.decision == "PENDING"
    assert at_deadline.decision == "PENDING"
    assert before_closure.decision == "PENDING"
    assert (at_closure.decision, at_closure.reason_code) == ("FAILED", reason)


def test_equal_provider_timestamp_precedence_is_deterministic():
    for order in permutations(
        (_evidence("sent", T1), _evidence("failed", T1), _evidence("read", T1))
    ):
        assert _reduce(order).decision == "PASSED"
    for order in permutations((_evidence("sent", T1), _evidence("failed", T1))):
        result = _reduce(order)
        assert (result.decision, result.reason_code) == ("FAILED", META_FAILED_REASON)


@pytest.mark.parametrize(
    "raw",
    [None, "", " ", "12.5", "invalid", -1, 1.5, True, False, "9" * 64, 10**20],
)
def test_missing_invalid_negative_fraction_boolean_and_overflow_never_participate(raw):
    assert parse_provider_timestamp(raw) is None
    pending = _reduce([_evidence("delivered", raw)], now=CLOSURE - timedelta(microseconds=1))
    closed = _reduce([_evidence("delivered", raw)])
    assert pending.decision == "PENDING"
    assert (closed.decision, closed.reason_code) == ("FAILED", META_INVALID_REASON)
    assert closed.delivery_proved is False


@pytest.mark.parametrize("raw", [0, "0", T1, str(T1)])
def test_strict_unix_seconds_are_normalized_to_utc(raw):
    parsed = parse_provider_timestamp(raw)
    assert parsed is not None
    assert parsed.tzinfo is UTC


def test_duplicate_and_different_failed_error_flags_do_not_change_reduction():
    evidences = [
        _evidence("failed", T1, errors_present=False),
        _evidence("failed", T1, errors_present=True),
    ]
    first = _reduce(evidences[:1])
    replay = _reduce(evidences)
    assert replay.decision == first.decision == "FAILED"
    assert replay.reason_code == first.reason_code == META_FAILED_REASON
    assert replay.latest_valid_provider_timestamp == first.latest_valid_provider_timestamp


def test_callback_after_deadline_is_audit_only_even_during_grace():
    late = _evidence(
        "delivered", T2, received_at=DEADLINE + timedelta(microseconds=1)
    )
    during_grace = _reduce([late], now=DEADLINE + timedelta(seconds=1))
    closed = _reduce([late])
    assert during_grace.decision == "PENDING"
    assert (closed.decision, closed.reason_code) == ("FAILED", META_TIMEOUT_REASON)
    assert closed.evidence_class == "LATE_ONLY"
    assert closed.delivery_proved is False


def test_callback_exactly_at_deadline_is_admitted_and_passes_during_grace():
    boundary = _evidence("delivered", T2, received_at=DEADLINE)
    result = _reduce([boundary], now=DEADLINE + timedelta(seconds=1))
    assert (result.decision, result.reason_code) == (
        "PASSED",
        META_DELIVERY_REASON,
    )
    assert result.due is False


def test_naive_local_times_are_normalized_without_changing_provider_order():
    result = reduce_meta_callback_evidence(
        [_evidence("failed", str(T1))],
        accepted_at=ACCEPTED.replace(tzinfo=None),
        now=CLOSURE.replace(tzinfo=None),
    )
    assert (result.decision, result.reason_code) == ("FAILED", META_FAILED_REASON)
