"""Application authority for V0.4A current client location snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from attention_router.config import Settings
from attention_router.core.client.location import (
    ClientLocationPrecision,
    ClientLocationValidationError,
    CurrentLocationObservation,
    require_current_location_observation,
)
from attention_router.infrastructure import (
    client_location_repository as location_repository,
)
from attention_router.infrastructure import client_session_repository as session_repository
from attention_router.application.client_session import (
    ClientSessionAuthorityRejected,
    ClientSessionGrant,
    ClientSessionState,
    ClientSessionUnauthenticated,
    _aware,
    _device_authority,
    _membership_authority,
)
from attention_router.core.client.session import (
    ClientSessionAuthorityError,
    require_client_session_authority,
)


class ClientLocationError(RuntimeError):
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
class ClientLocationSnapshot:
    location_snapshot_id: str
    human_identity_id: str
    device_id: str
    tenant_id: str
    latitude: float
    longitude: float
    accuracy_m: float
    precision: ClientLocationPrecision | None
    captured_at: datetime
    received_at: datetime


class ClientLocationService:
    def __init__(self, *, settings: Settings):
        self.settings = settings

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
        if not isinstance(session_token, str):
            raise ClientLocationUnauthenticated()
        try:
            session_row = session_repository.get_client_session_by_token(
                session,
                token=session_token,
            )
        except (UnicodeEncodeError, ValueError):
            raise ClientLocationUnauthenticated() from None
        if (
            session_row is None
            or session_row.state != "ACTIVE"
            or _aware(session_row.expires_at, now) <= now
        ):
            raise ClientLocationUnauthenticated()
        device = session_repository.get_client_device(
            session,
            device_id=session_row.device_id,
        )
        membership = session_repository.get_membership(
            session,
            human_identity_id=session_row.human_identity_id,
            tenant_id=session_row.tenant_id,
        )
        tenant = session_repository.get_tenant(
            session,
            tenant_id=session_row.tenant_id,
        )
        if device is None or membership is None or tenant is None:
            raise ClientLocationAuthorityRejected()
        grant = ClientSessionGrant(
            session_id=session_row.id,
            human_identity_id=session_row.human_identity_id,
            device_id=session_row.device_id,
            tenant_id=session_row.tenant_id,
            state=ClientSessionState(session_row.state),
            expires_at=_aware(session_row.expires_at, now),
        )
        try:
            require_client_session_authority(
                session=grant,
                device=_device_authority(device),
                membership=_membership_authority(membership),
                tenant_active=tenant.status == "ACTIVE",
                now=now,
            )
        except (ValueError, ClientSessionAuthorityError) as error:
            raise ClientLocationAuthorityRejected() from error
        return session_row

    def put_current(
        self,
        session: Session,
        *,
        session_token: str | None,
        observation: CurrentLocationObservation,
        now: datetime | None = None,
    ) -> ClientLocationSnapshot:
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
        row = location_repository.upsert_current_location(
            session,
            human_identity_id=authority.human_identity_id,
            device_id=authority.device_id,
            tenant_id=authority.tenant_id,
            latitude=observation.latitude,
            longitude=observation.longitude,
            accuracy_m=observation.accuracy_m,
            precision=(
                observation.precision.value
                if observation.precision is not None
                else None
            ),
            captured_at=observation.captured_at,
            now=current,
        )
        return _snapshot(row)

    def get_current(
        self,
        session: Session,
        *,
        session_token: str | None,
        now: datetime | None = None,
    ) -> ClientLocationSnapshot:
        self._require_enabled()
        current = now if now is not None else datetime.now(UTC)
        authority = self._authority(
            session,
            session_token=session_token,
            now=current,
        )
        row = location_repository.get_current_location(
            session,
            device_id=authority.device_id,
            tenant_id=authority.tenant_id,
        )
        if row is None:
            raise ClientLocationNotFound()
        if row.human_identity_id != authority.human_identity_id:
            raise ClientLocationAuthorityRejected()
        return _snapshot(row)


def _snapshot(row) -> ClientLocationSnapshot:
    return ClientLocationSnapshot(
        location_snapshot_id=row.id,
        human_identity_id=row.human_identity_id,
        device_id=row.device_id,
        tenant_id=row.tenant_id,
        latitude=row.latitude,
        longitude=row.longitude,
        accuracy_m=row.accuracy_m,
        precision=(
            ClientLocationPrecision(row.precision)
            if row.precision is not None
            else None
        ),
        captured_at=row.captured_at,
        received_at=row.received_at,
    )
