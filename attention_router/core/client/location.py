"""Infrastructure-independent V0.4A current-location validation vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from math import isfinite


MAX_LOCATION_AGE_SECONDS = 600
MAX_LOCATION_FUTURE_SKEW_SECONDS = 120
MAX_LOCATION_ACCURACY_METERS = 10_000.0


class ClientLocationPrecision(StrEnum):
    PRECISE = "PRECISE"
    APPROXIMATE = "APPROXIMATE"


@dataclass(frozen=True, slots=True)
class CurrentLocationObservation:
    latitude: float
    longitude: float
    accuracy_m: float
    captured_at: datetime
    precision: ClientLocationPrecision | None = None


@dataclass(frozen=True, slots=True)
class ClientLocationValidationDecision:
    allowed: bool
    reason_code: str


class ClientLocationValidationError(ValueError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def evaluate_current_location_observation(
    observation: CurrentLocationObservation,
    *,
    now: datetime,
) -> ClientLocationValidationDecision:
    if observation.captured_at.tzinfo is None or now.tzinfo is None:
        return ClientLocationValidationDecision(False, "INVALID_TIME_CONTEXT")
    if not isfinite(observation.latitude) or not -90.0 <= observation.latitude <= 90.0:
        return ClientLocationValidationDecision(False, "LATITUDE_INVALID")
    if not isfinite(observation.longitude) or not -180.0 <= observation.longitude <= 180.0:
        return ClientLocationValidationDecision(False, "LONGITUDE_INVALID")
    if (
        not isfinite(observation.accuracy_m)
        or observation.accuracy_m <= 0
        or observation.accuracy_m > MAX_LOCATION_ACCURACY_METERS
    ):
        return ClientLocationValidationDecision(False, "ACCURACY_INVALID")
    if observation.captured_at > now + timedelta(seconds=MAX_LOCATION_FUTURE_SKEW_SECONDS):
        return ClientLocationValidationDecision(False, "LOCATION_FUTURE")
    if now - observation.captured_at > timedelta(seconds=MAX_LOCATION_AGE_SECONDS):
        return ClientLocationValidationDecision(False, "LOCATION_STALE")
    return ClientLocationValidationDecision(True, "LOCATION_ACCEPTED")


def require_current_location_observation(
    observation: CurrentLocationObservation,
    *,
    now: datetime,
) -> ClientLocationValidationDecision:
    decision = evaluate_current_location_observation(observation, now=now)
    if not decision.allowed:
        raise ClientLocationValidationError(decision.reason_code)
    return decision
