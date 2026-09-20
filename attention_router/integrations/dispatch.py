from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.platform.events import record_timeline_event
from attention_router.core.events import EventOrigin
from attention_router.domain.models import now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.integrations.tenant_binding import INBOUND_SCOPE
from attention_router.infrastructure.models import (
    ActorBindingRow,
    CanonicalEventRow,
    IntegrationBindingRow,
    IntegrationInboxRow,
    TenantRow,
)


@dataclass(frozen=True, slots=True)
class IntegrationDispatchResult:
    selected: int = 0
    processed: int = 0
    blocked: int = 0


class IntegrationDispatchBlocked(RuntimeError):
    pass


def integration_actor_binding_source(binding_id: str) -> str:
    source = f"integration:{binding_id}"
    if len(source) > 120:
        raise ValueError("INTEGRATION_ACTOR_BINDING_SOURCE_TOO_LONG")
    return source


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_datetime(value: Any, *, reason: str) -> datetime:
    if not isinstance(value, str):
        raise IntegrationDispatchBlocked(reason)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntegrationDispatchBlocked(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IntegrationDispatchBlocked(reason)
    return parsed.astimezone(UTC)


def _validated_payload(
    row: IntegrationInboxRow,
    binding: IntegrationBindingRow,
) -> dict[str, Any]:
    if hashlib.sha256(row.raw_body).hexdigest() != row.body_sha256:
        raise IntegrationDispatchBlocked("INTEGRATION_BODY_FINGERPRINT_MISMATCH")
    try:
        payload = json.loads(row.raw_body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise IntegrationDispatchBlocked("INTEGRATION_BODY_CORRUPT") from exc
    if not isinstance(payload, dict):
        raise IntegrationDispatchBlocked("INTEGRATION_BODY_CORRUPT")

    source = payload.get("source")
    if not isinstance(source, dict):
        raise IntegrationDispatchBlocked("INTEGRATION_SOURCE_CORRUPT")
    account_key = source.get("account_id") or ""
    expected = (
        row.tenant_id,
        binding.kind,
        binding.name,
        binding.instance_id,
        binding.account_key,
    )
    actual = (
        payload.get("tenant_id"),
        source.get("kind"),
        source.get("name"),
        source.get("instance_id"),
        account_key,
    )
    if actual != expected:
        raise IntegrationDispatchBlocked("INTEGRATION_BINDING_DRIFT")
    if (
        payload.get("contract_type") != "inbound_event"
        or payload.get("schema_version") != "1"
        or payload.get("external_event_id") != row.external_event_id
        or payload.get("idempotency_key") != row.idempotency_key
        or payload.get("correlation_id") != row.correlation_id
    ):
        raise IntegrationDispatchBlocked("INTEGRATION_RECEIPT_IDENTITY_MISMATCH")
    return payload


def _resolve_actor(
    session: Session,
    *,
    row: IntegrationInboxRow,
    payload: dict[str, Any],
) -> str | None:
    actor = payload.get("actor")
    if not isinstance(actor, dict):
        return None
    external_actor_id = actor.get("external_actor_id")
    if (
        not isinstance(external_actor_id, str)
        or not external_actor_id
        or len(external_actor_id) > 180
    ):
        return None

    source = integration_actor_binding_source(row.binding_id)
    binding = session.scalar(
        select(ActorBindingRow).where(
            ActorBindingRow.tenant_id == row.tenant_id,
            ActorBindingRow.source == source,
            ActorBindingRow.external_actor_id == external_actor_id,
            ActorBindingRow.is_active.is_(True),
        )
    )
    return binding.actor_key if binding is not None else None


def _canonical_event_id(inbox_id: str) -> str:
    return "intg-" + stable_hash(
        {"kind": "integration_inbox", "inbox_id": inbox_id}
    )[:48]


def _timeline_type(event_type: str) -> str:
    if event_type == "message":
        return "MESSAGE_RECEIVED"
    if event_type == "call":
        return "CALL_EVENT_RECEIVED"
    return "INTEGRATION_EVENT_RECEIVED"


def _process_row(
    session: Session,
    *,
    row: IntegrationInboxRow,
    stamp: datetime,
) -> CanonicalEventRow:
    tenant = session.get(TenantRow, row.tenant_id)
    if tenant is None or tenant.status != "ACTIVE":
        raise IntegrationDispatchBlocked("INTEGRATION_TENANT_INACTIVE")

    binding = session.get(IntegrationBindingRow, row.binding_id)
    if (
        binding is None
        or binding.tenant_id != row.tenant_id
        or not binding.active
    ):
        raise IntegrationDispatchBlocked("INTEGRATION_BINDING_INACTIVE")
    if (
        type(binding.scopes) is not list
        or INBOUND_SCOPE not in binding.scopes
    ):
        raise IntegrationDispatchBlocked("INTEGRATION_BINDING_SCOPE_INACTIVE")

    payload = _validated_payload(row, binding)
    actor_id = _resolve_actor(session, row=row, payload=payload)
    event_id = _canonical_event_id(row.id)
    existing = session.get(CanonicalEventRow, event_id)

    if existing is not None:
        if (
            existing.tenant_id != row.tenant_id
            or (existing.payload_ref or {}).get("integration_inbox_id") != row.id
        ):
            raise IntegrationDispatchBlocked(
                "INTEGRATION_CANONICAL_ID_CONFLICT"
            )
        canonical = existing
    else:
        occurred_at = _parse_datetime(
            payload.get("occurred_at"),
            reason="INTEGRATION_OCCURRED_AT_INVALID",
        )
        received_at = _parse_datetime(
            payload.get("received_at"),
            reason="INTEGRATION_RECEIVED_AT_INVALID",
        )
        thread = payload.get("thread")
        actor = payload.get("actor")
        artifacts = payload.get("artifact_ids")
        if not isinstance(artifacts, list):
            raise IntegrationDispatchBlocked("INTEGRATION_ARTIFACT_IDS_INVALID")

        event_type = payload.get("event_type")
        payload_type = payload.get("payload_type")
        if (
            not isinstance(event_type, str)
            or not event_type
            or len(event_type) > 120
            or not isinstance(payload_type, str)
            or not payload_type
            or len(payload_type) > 80
        ):
            raise IntegrationDispatchBlocked(
                "INTEGRATION_CANONICAL_FIELDS_INVALID"
            )

        source_name = binding.name
        canonical = CanonicalEventRow(
            id=event_id,
            tenant_id=row.tenant_id,
            origin=(
                EventOrigin.EXTERNAL_INBOUND.value
                if binding.kind == "CHANNEL"
                else EventOrigin.PROVIDER_EVENT.value
            ),
            event_type=event_type,
            actor_id=actor_id,
            resource_id=None,
            channel=source_name if len(source_name) <= 80 else "integration",
            payload_type=payload_type,
            payload_ref={"integration_inbox_id": row.id},
            occurred_at=occurred_at,
            received_at=received_at,
            correlation_id=row.correlation_id,
            causation_id=payload.get("causation_id"),
            inbound_event_id=None,
            metadata_sanitized={
                "integration_binding_id": row.binding_id,
                "integration_kind": binding.kind,
                "integration_name": binding.name,
                "integration_instance_id": binding.instance_id,
                "integration_account_present": bool(binding.account_key),
                "external_actor_present": isinstance(actor, dict),
                "actor_resolved": actor_id is not None,
                "thread_present": isinstance(thread, dict),
                "thread_kind": (
                    thread.get("kind")
                    if isinstance(thread, dict)
                    and isinstance(thread.get("kind"), str)
                    else None
                ),
                "artifact_count": len(artifacts),
                "contract_schema_version": "1",
            },
            lineage_classification="ORGANIC",
            scenario_run_id=None,
            scenario_step_run_id=None,
        )
        session.add(canonical)
        session.flush()

    record_timeline_event(
        session,
        tenant_id=row.tenant_id,
        canonical_event_id=canonical.id,
        actor_id=canonical.actor_id,
        event_type=_timeline_type(canonical.event_type),
        occurred_at=_utc(canonical.occurred_at),
        provenance="integration_dispatch",
        event_ref={
            "canonical_event_id": canonical.id,
            "integration_inbox_id": row.id,
        },
        visibility="PRIVATE",
        metadata={
            "integration_name": binding.name,
            "actor_resolved": canonical.actor_id is not None,
        },
    )
    row.state = "PROCESSED"
    row.canonical_event_id = canonical.id
    row.processed_at = stamp
    row.dispatch_reason = None
    session.flush()
    return canonical


def process_integration_inbox(
    session: Session,
    *,
    limit: int = 20,
    now: datetime | None = None,
) -> IntegrationDispatchResult:
    """Atomically bridge admitted neutral events into Canonical Event + Timeline."""

    if limit < 1 or limit > 200:
        raise ValueError("INTEGRATION_DISPATCH_LIMIT_OUT_OF_RANGE")
    stamp = _utc(now or now_utc())

    rows = session.scalars(
        select(IntegrationInboxRow)
        .where(IntegrationInboxRow.state == "PENDING")
        .order_by(
            IntegrationInboxRow.admitted_at,
            IntegrationInboxRow.id,
        )
        .with_for_update(skip_locked=True)
        .limit(limit)
    ).all()

    processed = 0
    blocked = 0
    for row in rows:
        try:
            _process_row(session, row=row, stamp=stamp)
        except IntegrationDispatchBlocked as exc:
            row.state = "BLOCKED"
            row.canonical_event_id = None
            row.processed_at = stamp
            row.dispatch_reason = str(exc)[:120]
            session.flush()
            blocked += 1
        else:
            processed += 1

    return IntegrationDispatchResult(
        selected=len(rows),
        processed=processed,
        blocked=blocked,
    )


__all__ = [
    "IntegrationDispatchBlocked",
    "IntegrationDispatchResult",
    "integration_actor_binding_source",
    "process_integration_inbox",
]
