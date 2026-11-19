"""Historical observations never bypass current-location temporal validation."""

from datetime import UTC, datetime, timedelta
import hashlib
import json
from sqlalchemy import select
from attention_router.application.client_location import (
    ClientLocationService,
    ClientLocationAuthorityRejected,
)
from attention_router.infrastructure.client_bootstrap_models import ClientDeviceRow
from attention_router.infrastructure.location_history_models import LocationObservationRow
from attention_router.infrastructure import client_location_repository as current_repo
from attention_router.core.client.location import (
    CurrentLocationObservation,
    evaluate_current_location_observation,
)


class HistoryInvalid(ValueError):
    pass


class HistoryConflict(ValueError):
    pass


class LocationHistoryService(ClientLocationService):
    def ingest(self, session, *, session_token, batch, now=None):
        self._require_enabled()
        now = now or datetime.now(UTC)
        auth = self._authority(session, session_token=session_token, now=now)
        scope = (auth.active_tenant_id, auth.human_identity_id, auth.device.device_id)
        if scope != (batch.tenant_id, batch.human_identity_id, batch.device_id):
            raise ClientLocationAuthorityRejected()
        owners = [
            m
            for m in auth.memberships
            if m.tenant_id == scope[0] and m.role == "OWNER" and m.status == "ACTIVE"
        ]
        if len(owners) != 1:
            raise ClientLocationAuthorityRejected()
        # Serialize admission for this device, including retries of the same batch.
        device = session.scalar(
            select(ClientDeviceRow)
            .where(ClientDeviceRow.id == scope[2])
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if device is None or device.status != "ACTIVE" or device.human_identity_id != scope[1]:
            raise ClientLocationAuthorityRejected()
        if any(p.captured_at.tzinfo is None for p in batch.observations):
            raise HistoryInvalid("LOCATION_HISTORY_TIME_INVALID")
        accepted = []
        for point in sorted(batch.observations, key=lambda p: p.captured_at):
            if (
                point.captured_at.tzinfo is None
                or point.captured_at < datetime(1970, 1, 1, tzinfo=UTC)
                or point.captured_at > now + timedelta(seconds=120)
            ):
                raise HistoryInvalid("LOCATION_HISTORY_TIME_INVALID")
            payload = point.model_dump(mode="json")
            payload["captured_at"] = point.captured_at.astimezone(UTC).isoformat()
            fingerprint = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            key = (scope[0], scope[2], str(point.observation_id))
            row = session.get(LocationObservationRow, key)
            if row is not None:
                if row.payload_sha256 != fingerprint or row.human_identity_id != scope[1]:
                    raise HistoryConflict("LOCATION_HISTORY_ID_CONFLICT")
            else:
                row = LocationObservationRow(
                    tenant_id=scope[0],
                    human_identity_id=scope[1],
                    device_id=scope[2],
                    observation_id=str(point.observation_id),
                    latitude=point.latitude,
                    longitude=point.longitude,
                    accuracy_m=point.accuracy_m,
                    precision=point.precision,
                    provider=point.provider,
                    captured_at=point.captured_at,
                    received_at=now,
                    payload_sha256=fingerprint,
                )
                session.add(row)
                session.flush()
                observation = CurrentLocationObservation(
                    point.latitude, point.longitude, point.accuracy_m, point.captured_at
                )
                if evaluate_current_location_observation(observation, now=now).allowed:
                    try:
                        current_repo.upsert_current_location(
                            session,
                            human_identity_id=scope[1],
                            device_id=scope[2],
                            tenant_id=scope[0],
                            latitude=point.latitude,
                            longitude=point.longitude,
                            accuracy_m=point.accuracy_m,
                            precision=point.precision,
                            captured_at=point.captured_at,
                            received_at=now,
                        )
                    except current_repo.LocationSnapshotStale:
                        pass  # accepted historical observation, newer current position preserved
            accepted.append(str(point.observation_id))
        return {
            "contract_version": "1",
            "acknowledged_ids": accepted,
            "received_at": now.isoformat(),
        }
