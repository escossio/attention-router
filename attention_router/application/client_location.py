"""V0.4A authenticated current-location snapshot service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from attention_router.application.client_session import (
    ClientSessionAuthorityRejected,
    ClientSessionError,
    ClientSessionService,
    ClientSessionUnauthenticated,
)
from attention_router.config import Settings
from attention_router.core.client.location import (
    ClientLocationValidationError,
    CurrentLocationObservation,
    require_current_location_observation,
)
from attention_router.infrastructure import client_location_repository as repository


class ClientLocationError(Exception):
    code = "CLIENT_LOCATION_UNAVAILABLE"


class ClientLocationDisabled(ClientLocationError):
    code = "CLIENT_LOCATION_DISABLED"


class ClientLocationInvalid(ClientLocationError):
    code = "CLIENT_LOCATION_INVALID"


class ClientLocationStale(ClientLocationError):
    code = "CLIENT_LOCATION_STALE"


class ClientLocationFuture(ClientLocationError):
    code = "CLIENT_LOCATION_FUTURE"


class ClientLocationUnauthenticated(ClientLocationError):
    code = "CLIENT_LOCATION_UNAUTHENTICATED"


class ClientLocationAuthorityRejected(ClientLocationError):
    code = "CLIENT_LOCATION_AUTHORITY_REJECTED"


class ClientLocationNotFound(ClientLocationError):
    code = "CLIENT_LOCATION_NOT_FOUND"


class ClientLocationUnavailable(ClientLocationError):
    code = "CLIENT_LOCATION_UNAVAILABLE"
@dataclass(frozen=True, slots=True)
class ClientLocationSnapshotResult:
    location_snapshot_id: str
    human_identity_id: str
    device_id: str
    tenant_id: str
    latitude: float
    longitude: float
    accuracy_m: float
    precision: str | None
    captured_at: datetime
    received_at: datetime
    contract_version: str = "1"


def _aware(value: datetime, reference: datetime) -> datetime:
    return value.replace(tzinfo=reference.tzinfo or UTC) if value.tzinfo is None else value


class ClientLocationService:
    def __init__(
        self,
        *,
        settings: Settings,
        session_service: ClientSessionService,
    ):
        self.settings = settings
        self.session_service = session_service

    def _require_enabled(self) -> None:
        if not self.settings.client_location_enabled:
            raise ClientLocationDisabled()

    def _authority(
        self,
        session: Session,
        *,
        session_token: str | None,
        now: datetime,
    ):
        try:
            return self.session_service.authenticated_bootstrap(
                session,
                session_token=session_token,
                now=now,
            )
        except ClientSessionUnauthenticated as error:
            raise ClientLocationUnauthenticated() from error
        except ClientSessionAuthorityRejected as error:
            raise ClientLocationAuthorityRejected() from error
        except ClientSessionError as error:
            raise ClientLocationUnavailable() from error

    def put_current(
        self,
        session: Session,
        *,
        session_token: str | None,
        observation: CurrentLocationObservation,
        now: datetime | None = None,
    ) -> ClientLocationSnapshotResult:
        self._require_enabled()
        current = now if now is not None else datetime.now(UTC)
        authority = self._authority(
            session,
            session_token=session_token,
            now=current,
        )
        try:
            require_current_location_observation(observation, now=current)
        except ClientLocationValidationError as error:
            if error.reason_code == "LOCATION_STALE":
                raise ClientLocationStale() from error
            if error.reason_code == "LOCATION_FUTURE":
                raise ClientLocationFuture() from error
            raise ClientLocationInvalid() from error
        try:
            row = repository.upsert_current_location(
                session,
                human_identity_id=authority.human_identity_id,
                device_id=authority.device.device_id,
                tenant_id=authority.active_tenant_id,
                latitude=observation.latitude,
                longitude=observation.longitude,
                accuracy_m=observation.accuracy_m,
                precision=(
                    observation.precision.value
                    if observation.precision is not None
                    else None
                ),
                captured_at=observation.captured_at,
                received_at=current,
            )
        except repository.LocationSnapshotStale as error:
            raise ClientLocationStale() from error
        except repository.LocationSnapshotConflict as error:
            raise ClientLocationUnavailable() from error
        return self._result(row, reference=current)

    def get_current(
        self,
        session: Session,
        *,
        session_token: str | None,
        now: datetime | None = None,
    ) -> ClientLocationSnapshotResult:
        self._require_enabled()
        current = now if now is not None else datetime.now(UTC)
        authority = self._authority(
            session,
            session_token=session_token,
            now=current,
        )
        row = repository.get_current_location(
            session,
            device_id=authority.device.device_id,
            tenant_id=authority.active_tenant_id,
        )
        if row is None:
            raise ClientLocationNotFound()
        if row.human_identity_id != authority.human_identity_id:
            raise ClientLocationAuthorityRejected()
        return self._result(row, reference=current)

    @staticmethod
    def _result(
        row,
        *,
        reference: datetime,
    ) -> ClientLocationSnapshotResult:
        return ClientLocationSnapshotResult(
            location_snapshot_id=row.id,
            human_identity_id=row.human_identity_id,
            device_id=row.device_id,
            tenant_id=row.tenant_id,
            latitude=row.latitude,
            longitude=row.longitude,
            accuracy_m=row.accuracy_m,
            precision=row.precision,
            captured_at=_aware(row.captured_at, reference),
            received_at=_aware(row.received_at, reference),
        )
