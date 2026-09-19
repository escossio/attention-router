"""Caller-transaction-owned persistence for V0.4A current location."""

from __future__ import annotations

from datetime import UTC, datetime
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from attention_router.infrastructure.client_location_models import (
    ClientLocationSnapshotRow,
)


class LocationSnapshotConflict(RuntimeError):
    pass


class LocationSnapshotStale(RuntimeError):
    pass


def _aware(value: datetime, reference: datetime) -> datetime:
    return (
        value.replace(tzinfo=reference.tzinfo or UTC)
        if value.tzinfo is None
        else value
    )


def get_current_location(
    session: Session,
    *,
    device_id: str,
    tenant_id: str,
) -> ClientLocationSnapshotRow | None:
    return session.scalar(
        select(ClientLocationSnapshotRow).where(
            ClientLocationSnapshotRow.device_id == device_id,
            ClientLocationSnapshotRow.tenant_id == tenant_id,
        )
    )


def lock_current_location(
    session: Session,
    *,
    device_id: str,
    tenant_id: str,
) -> ClientLocationSnapshotRow | None:
    return session.scalar(
        select(ClientLocationSnapshotRow)
        .where(
            ClientLocationSnapshotRow.device_id == device_id,
            ClientLocationSnapshotRow.tenant_id == tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
def upsert_current_location(
    session: Session,
    *,
    human_identity_id: str,
    device_id: str,
    tenant_id: str,
    latitude: float,
    longitude: float,
    accuracy_m: float,
    precision: str | None,
    captured_at: datetime,
    received_at: datetime,
) -> ClientLocationSnapshotRow:
    row = lock_current_location(
        session,
        device_id=device_id,
        tenant_id=tenant_id,
    )
    if row is not None:
        if captured_at < _aware(row.captured_at, captured_at):
            raise LocationSnapshotStale()
        row.human_identity_id = human_identity_id
        row.latitude = latitude
        row.longitude = longitude
        row.accuracy_m = accuracy_m
        row.precision = precision
        row.captured_at = captured_at
        row.received_at = received_at
        session.flush()
        return row

    row = ClientLocationSnapshotRow(
        id="cloc_" + secrets.token_urlsafe(24),
        human_identity_id=human_identity_id,
        device_id=device_id,
        tenant_id=tenant_id,
        latitude=latitude,
        longitude=longitude,
        accuracy_m=accuracy_m,
        precision=precision,
        captured_at=captured_at,
        received_at=received_at,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
        return row
    except IntegrityError:
        existing = lock_current_location(
            session,
            device_id=device_id,
            tenant_id=tenant_id,
        )
        if existing is None:
            raise LocationSnapshotConflict()
        if captured_at < _aware(existing.captured_at, captured_at):
            raise LocationSnapshotStale()
        existing.human_identity_id = human_identity_id
        existing.latitude = latitude
        existing.longitude = longitude
        existing.accuracy_m = accuracy_m
        existing.precision = precision
        existing.captured_at = captured_at
        existing.received_at = received_at
        session.flush()
        return existing
