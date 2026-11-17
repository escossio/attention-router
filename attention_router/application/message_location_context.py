"""Read-only correlation of an owner-authored message to measured Android device GPS.

A location observed on one device is NOT proof that the human owner or
the WhatsApp sending device was there. A clock window is never interpolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.infrastructure.client_bootstrap_models import (
    ClientDeviceRow,
    ClientTenantMembershipRow,
)
from attention_router.infrastructure.client_location_history_models import (
    ClientLocationObservationRow,
)
from attention_router.infrastructure.models import ConversationMessageRow


MAX_TIME_DELTA_SECONDS = 60
MAX_ACCURACY_FOR_MATCH_METERS = 250.0


@dataclass(frozen=True, slots=True)
class MessageLocationContext:
    message_id: str
    status: str
    evidence_basis: str
    message_at: datetime | None
    location_observation_id: str | None = None
    location_device_id: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    accuracy_m: float | None = None
    precision: str | None = None
    captured_at: datetime | None = None
    delta_seconds: float | None = None


class MessageLocationAccessDenied(RuntimeError):
    pass


class MessageLocationNotFound(RuntimeError):
    pass


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def correlate_message_with_owner_device_location(
    session: Session,
    *,
    tenant_id: str,
    authenticated_human_identity_id: str,
    message_id: str,
) -> MessageLocationContext:
    """Tenant-scoped evidence only; no writes, route inference, or geo lookup."""
    owners = session.scalars(
        select(ClientTenantMembershipRow).where(
            ClientTenantMembershipRow.tenant_id == tenant_id,
            ClientTenantMembershipRow.role == "OWNER",
            ClientTenantMembershipRow.status == "ACTIVE",
        )
    ).all()
    if len(owners) != 1 or owners[0].human_identity_id != authenticated_human_identity_id:
        raise MessageLocationAccessDenied("LOCATION_CORRELATION_OWNER_ONLY")

    message = session.scalar(
        select(ConversationMessageRow).where(
            ConversationMessageRow.id == message_id,
            ConversationMessageRow.tenant_id == tenant_id,
        )
    )
    if message is None:
        raise MessageLocationNotFound()

    at = _aware(message.sent_at).astimezone(UTC)
    metadata = message.metadata_json if isinstance(message.metadata_json, dict) else {}
    base = {
        "message_id": message.id,
        "message_at": at,
        "evidence_basis": "ANDROID_DEVICE_POSITION_NEAR_WHATSAPP_MESSAGE_TIME",
    }
    if (
        message.source != "wwebjs"
        or message.from_me is not True
        or message.direction != "OUTBOUND"
        or metadata.get("speaker_basis") != "REPRESENTED_OWNER"
        or metadata.get("capture_contract") != "LIVE_CONVERSATION_ARCHIVE_V0"
    ):
        return MessageLocationContext(status="AUTHORSHIP_NOT_PROVEN", **base)

    allowed_device_ids = set(session.scalars(
        select(ClientDeviceRow.id).where(
            ClientDeviceRow.human_identity_id == authenticated_human_identity_id,
            ClientDeviceRow.status == "ACTIVE",
            ClientDeviceRow.platform == "ANDROID",
        )
    ).all())
    if not allowed_device_ids:
        return MessageLocationContext(status="NO_ACTIVE_DEVICE", **base)

    rows = session.scalars(select(ClientLocationObservationRow).where(
        ClientLocationObservationRow.tenant_id == tenant_id,
        ClientLocationObservationRow.human_identity_id == authenticated_human_identity_id,
        ClientLocationObservationRow.device_id.in_(allowed_device_ids),
        ClientLocationObservationRow.captured_at >= at - timedelta(seconds=MAX_TIME_DELTA_SECONDS),
        ClientLocationObservationRow.captured_at <= at + timedelta(seconds=MAX_TIME_DELTA_SECONDS),
    ).order_by(ClientLocationObservationRow.captured_at)).all()
    if not rows:
        return MessageLocationContext(status="NO_TIME_MATCHED_LOCATION", **base)
    # A WhatsApp account can be operated from another machine. When multiple
    # Android devices have fixes near this moment, none is proven to be the
    # operator's or sending device's position.
    if len({row.device_id for row in rows}) != 1:
        return MessageLocationContext(status="AMBIGUOUS_DEVICES", **base)

    chosen = min(
        rows,
        key=lambda row: (
            abs((_aware(row.captured_at) - at).total_seconds()),
            row.accuracy_m,
            row.id,
        ),
    )
    if chosen.accuracy_m > MAX_ACCURACY_FOR_MATCH_METERS or chosen.precision != "PRECISE":
        return MessageLocationContext(
            status="LOCATION_TOO_IMPRECISE",
            accuracy_m=chosen.accuracy_m,
            precision=chosen.precision,
            **base,
        )
    delta = (_aware(chosen.captured_at).astimezone(UTC) - at).total_seconds()
    return MessageLocationContext(
        status="DEVICE_POSITION_NEAR_MESSAGE",
        location_observation_id=chosen.id,
        location_device_id=chosen.device_id,
        latitude=chosen.latitude,
        longitude=chosen.longitude,
        accuracy_m=chosen.accuracy_m,
        precision=chosen.precision,
        captured_at=_aware(chosen.captured_at).astimezone(UTC),
        delta_seconds=round(delta, 3),
        **base,
    )


__all__ = [
    "MAX_TIME_DELTA_SECONDS", "MAX_ACCURACY_FOR_MATCH_METERS",
    "MessageLocationContext", "MessageLocationAccessDenied",
    "MessageLocationNotFound", "correlate_message_with_owner_device_location",
]
