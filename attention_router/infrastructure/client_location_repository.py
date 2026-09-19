"""Caller-transaction-owned persistence for V0.4A current location."""

from __future__ import annotations

from datetime import datetime
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.infrastructure.client_location_models import (
    ClientLocationSnapshotRow,
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
    now: datetime,
) -> ClientLocationSnapshotRow:
    row = session.scalar(
        select(ClientLocationSnapshotRow)
        .where(
            ClientLocationSnapshotRow.device_id == device_id,
            ClientLocationSnapshotRow.tenant_id == tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
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
            received_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.human_identity_id = human_identity_id
        row.latitude = latitude
        row.longitude = longitude
        row.accuracy_m = accuracy_m
        row.precision = precision
        row.captured_at = captured_at
        row.updated_at = now
    session.flush()
    return row
