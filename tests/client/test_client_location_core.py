from datetime import UTC, datetime, timedelta
from math import nan

import pytest

from attention_router.core.client.location import (
    ClientLocationPrecision,
    ClientLocationValidationError,
    CurrentLocationObservation,
    evaluate_current_location_observation,
    require_current_location_observation,
)


NOW = datetime(2026, 9, 18, 20, 30, tzinfo=UTC)


def observation(**overrides):
    values = {
        "latitude": -3.73,
        "longitude": -38.54,
        "accuracy_m": 12.5,
        "captured_at": NOW - timedelta(seconds=5),
        "precision": ClientLocationPrecision.PRECISE,
    }
    values.update(overrides)
    return CurrentLocationObservation(**values)


def test_bounded_current_location_is_accepted():
    decision = require_current_location_observation(observation(), now=NOW)
    assert decision.allowed is True
    assert decision.reason_code == "LOCATION_ACCEPTED"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (observation(latitude=91.0), "LATITUDE_INVALID"),
        (observation(latitude=nan), "LATITUDE_INVALID"),
        (observation(longitude=-181.0), "LONGITUDE_INVALID"),
        (observation(longitude=nan), "LONGITUDE_INVALID"),
        (observation(accuracy_m=0.0), "ACCURACY_INVALID"),
        (observation(accuracy_m=10_001.0), "ACCURACY_INVALID"),
        (observation(accuracy_m=nan), "ACCURACY_INVALID"),
        (observation(captured_at=NOW + timedelta(seconds=121)), "LOCATION_FUTURE"),
        (observation(captured_at=NOW - timedelta(seconds=601)), "LOCATION_STALE"),
        (observation(captured_at=NOW.replace(tzinfo=None)), "INVALID_TIME_CONTEXT"),
    ],
)
def test_location_validation_fails_closed(value, reason):
    decision = evaluate_current_location_observation(value, now=NOW)
    assert decision.allowed is False
    assert decision.reason_code == reason
    with pytest.raises(ClientLocationValidationError, match=reason):
        require_current_location_observation(value, now=NOW)


def test_now_must_be_timezone_aware():
    decision = evaluate_current_location_observation(
        observation(),
        now=NOW.replace(tzinfo=None),
    )
    assert decision.allowed is False
    assert decision.reason_code == "INVALID_TIME_CONTEXT"


def test_approximate_location_is_a_valid_user_choice():
    assert require_current_location_observation(
        observation(precision=ClientLocationPrecision.APPROXIMATE),
        now=NOW,
    ).allowed
