"""Personal Context V2B owner-initiated semantic bootstrap lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from attention_router.application.memory import (
    HistoryAdapter,
    HistoryBackfillService,
)
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.client_bootstrap_models import (
    ClientTenantMembershipRow,
)
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import ActorBindingRow, TenantRow
from attention_router.infrastructure.personal_context_bootstrap_models import (
    PersonalContextBootstrapBatchRow,
    PersonalContextBootstrapRunRow,
)


SUPPORTED_BOOTSTRAP_SOURCES = frozenset({"WHATSAPP_TEXT", "GMAIL_TEXT"})
_ARCHIVE_SOURCE_BY_KIND = {
    "WHATSAPP_TEXT": "whatsapp",
    "GMAIL_TEXT": "gmail",
}
BOOTSTRAP_MODE = "HISTORICAL_BOOTSTRAP"
_TERMINAL_STATES = frozenset({"COMPLETED", "CANCELLED", "FAILED"})
_STABLE_DISCOVERY_METRICS = frozenset(
    {"total_chats_discovered", "direct_chats", "group_chats"}
)
_DEFAULT_BUDGET = {
    "page_size": 50,
    "max_messages_per_chat": 100,
    "max_total_messages": 500,
}


class PersonalContextBootstrapError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BootstrapAdvanceResult:
    run_id: str
    state: str
    batch_id: str | None
    resume_cursor: dict[str, Any] | None
    metrics: dict[str, Any]


def _utc(value: datetime | None = None) -> datetime:
    stamp = value or now_utc()
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def _bounded_text(
    value: str | None,
    *,
    field: str,
    limit: int,
    allow_none: bool = False,
) -> str | None:
    if value is None:
        if allow_none:
            return None
        raise PersonalContextBootstrapError(f"{field}_REQUIRED")
    normalized = " ".join(value.split())
    if not normalized:
        if allow_none:
            return None
        raise PersonalContextBootstrapError(f"{field}_REQUIRED")
    if len(normalized) > limit:
        raise PersonalContextBootstrapError(f"{field}_TOO_LONG")
    return normalized


def _normalized_selection(value: dict[str, Any] | None) -> dict[str, Any]:
    selection = dict(value or {})
    unknown = set(selection) - {"chat_keys"}
    if unknown:
        raise PersonalContextBootstrapError(
            "BOOTSTRAP_SOURCE_SELECTION_FIELD_UNSUPPORTED"
        )
    chat_keys = selection.get("chat_keys")
    if chat_keys is None:
        return {}
    if (
        not isinstance(chat_keys, list)
        or not chat_keys
        or len(chat_keys) > 5000
        or any(
            not isinstance(item, str)
            or not item.strip()
            or len(item.strip()) > 240
            for item in chat_keys
        )
    ):
        raise PersonalContextBootstrapError(
            "BOOTSTRAP_SOURCE_SELECTION_INVALID"
        )
    deduplicated = sorted({item.strip() for item in chat_keys})
    return {"chat_keys": deduplicated}


def _normalized_budget(value: dict[str, Any] | None) -> dict[str, int]:
    budget = dict(_DEFAULT_BUDGET)
    if value:
        unknown = set(value) - set(_DEFAULT_BUDGET)
        if unknown:
            raise PersonalContextBootstrapError(
                "BOOTSTRAP_PROCESSING_BUDGET_FIELD_UNSUPPORTED"
            )
        budget.update(value)

    constraints = {
        "page_size": (1, 200),
        "max_messages_per_chat": (1, 1000),
        "max_total_messages": (1, 5000),
    }
    for key, (minimum, maximum) in constraints.items():
        item = budget.get(key)
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or item < minimum
            or item > maximum
        ):
            raise PersonalContextBootstrapError(
                "BOOTSTRAP_PROCESSING_BUDGET_INVALID"
            )
    return budget


def resolve_represented_owner_actor_key(
    session: Session,
    *,
    tenant_id: str,
    owner_human_identity_id: str,
) -> str:
    """Resolve the one active owner actor key for an authenticated owner scope."""
    tenant = session.get(TenantRow, tenant_id)
    if tenant is None or tenant.status != "ACTIVE":
        raise PersonalContextBootstrapError("BOOTSTRAP_TENANT_UNAVAILABLE")

    membership = session.scalar(
        select(ClientTenantMembershipRow).where(
            ClientTenantMembershipRow.tenant_id == tenant_id,
            ClientTenantMembershipRow.human_identity_id
            == owner_human_identity_id,
            ClientTenantMembershipRow.role == "OWNER",
            ClientTenantMembershipRow.status == "ACTIVE",
        )
    )
    if membership is None:
        raise PersonalContextBootstrapError(
            "BOOTSTRAP_OWNER_MEMBERSHIP_REQUIRED"
        )

    bindings = list(
        session.scalars(
            select(ActorBindingRow).where(
                ActorBindingRow.tenant_id == tenant_id,
                ActorBindingRow.is_active.is_(True),
                or_(
                    ActorBindingRow.actor_category == "owner",
                    ActorBindingRow.binding_metadata["owner"].as_boolean().is_(True),
                ),
            )
        ).all()
    )
    owner_actor_keys = {binding.actor_key for binding in bindings}
    if len(owner_actor_keys) != 1:
        raise PersonalContextBootstrapError(
            "BOOTSTRAP_REPRESENTED_OWNER_AMBIGUOUS"
        )
    return next(iter(owner_actor_keys))


def _require_owner_scope(
    session: Session,
    *,
    tenant_id: str,
    owner_human_identity_id: str,
    represented_owner_actor_key: str,
) -> None:
    owner_actor_key = resolve_represented_owner_actor_key(
        session,
        tenant_id=tenant_id,
        owner_human_identity_id=owner_human_identity_id,
    )
    if represented_owner_actor_key != owner_actor_key:
        raise PersonalContextBootstrapError(
            "BOOTSTRAP_REPRESENTED_OWNER_UNRESOLVED"
        )


def create_bootstrap_run(
    session: Session,
    *,
    tenant_id: str,
    owner_human_identity_id: str,
    represented_owner_actor_key: str,
    source_kind: str,
    consent_ref: str,
    source_account: str = "default",
    source_revision: str | None = None,
    source_selection: dict[str, Any] | None = None,
    processing_budget: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[PersonalContextBootstrapRunRow, bool]:
    """Create one idempotent bootstrap run from explicit owner consent."""
    _require_owner_scope(
        session,
        tenant_id=tenant_id,
        owner_human_identity_id=owner_human_identity_id,
        represented_owner_actor_key=represented_owner_actor_key,
    )
    source = source_kind.strip().upper()
    if source not in SUPPORTED_BOOTSTRAP_SOURCES:
        raise PersonalContextBootstrapError(
            "BOOTSTRAP_SOURCE_KIND_UNSUPPORTED"
        )
    account = _bounded_text(
        source_account,
        field="BOOTSTRAP_SOURCE_ACCOUNT",
        limit=180,
    )
    consent = _bounded_text(
        consent_ref,
        field="BOOTSTRAP_CONSENT_REF",
        limit=240,
    )
    revision = _bounded_text(
        source_revision,
        field="BOOTSTRAP_SOURCE_REVISION",
        limit=160,
        allow_none=True,
    )
    selection = _normalized_selection(source_selection)
    budget = _normalized_budget(processing_budget)
    identity_payload = {
        "schema_version": "personal-context-bootstrap-v2b",
        "tenant_id": tenant_id,
        "owner_human_identity_id": owner_human_identity_id,
        "represented_owner_actor_key": represented_owner_actor_key,
        "source_kind": source,
        "source_account": account,
        "source_revision": revision,
        "source_selection": selection,
        "consent_ref": consent,
    }
    idempotency_key = (
        "personal-context-bootstrap:"
        + stable_hash(identity_payload)[:80]
    )
    existing = session.scalar(
        select(PersonalContextBootstrapRunRow).where(
            PersonalContextBootstrapRunRow.idempotency_key
            == idempotency_key
        )
    )
    if existing is not None:
        return existing, False

    stamp = _utc(now)
    row = PersonalContextBootstrapRunRow(
        id=new_id(),
        tenant_id=tenant_id,
        owner_human_identity_id=owner_human_identity_id,
        represented_owner_actor_key=represented_owner_actor_key,
        source_kind=source,
        source_account=account,
        source_revision=revision,
        source_selection=selection,
        consent_ref=consent,
        idempotency_key=idempotency_key,
        mode=BOOTSTRAP_MODE,
        state="CREATED",
        requested_control="NONE",
        resume_cursor=None,
        processing_budget=budget,
        progress={},
        failure_summary=None,
        created_at=stamp,
        updated_at=stamp,
        started_at=None,
        paused_at=None,
        completed_at=None,
        cancelled_at=None,
        failed_at=None,
    )
    session.add(row)
    session.flush()
    return row, True


def queue_bootstrap_run(
    session: Session,
    run_id: str,
    *,
    now: datetime | None = None,
) -> PersonalContextBootstrapRunRow:
    row = session.get(PersonalContextBootstrapRunRow, run_id)
    if row is None:
        raise PersonalContextBootstrapError("BOOTSTRAP_RUN_NOT_FOUND")
    if row.state == "QUEUED":
        return row
    if row.state != "CREATED":
        raise PersonalContextBootstrapError(
            "BOOTSTRAP_RUN_NOT_QUEUEABLE"
        )
    row.state = "QUEUED"
    row.updated_at = _utc(now)
    session.flush()
    return row


def request_bootstrap_control(
    session: Session,
    run_id: str,
    *,
    control: str,
    now: datetime | None = None,
) -> PersonalContextBootstrapRunRow:
    row = session.get(PersonalContextBootstrapRunRow, run_id)
    if row is None:
        raise PersonalContextBootstrapError("BOOTSTRAP_RUN_NOT_FOUND")
    requested = control.strip().upper()
    if requested not in {"PAUSE", "CANCEL"}:
        raise PersonalContextBootstrapError(
            "BOOTSTRAP_CONTROL_UNSUPPORTED"
        )
    if row.state in _TERMINAL_STATES:
        return row

    stamp = _utc(now)
    if row.state == "RUNNING":
        row.requested_control = requested
    elif requested == "PAUSE":
        row.state = "PAUSED"
        row.requested_control = "NONE"
        row.paused_at = stamp
    else:
        row.state = "CANCELLED"
        row.requested_control = "NONE"
        row.cancelled_at = stamp
    row.updated_at = stamp
    session.flush()
    return row


def resume_bootstrap_run(
    session: Session,
    run_id: str,
    *,
    now: datetime | None = None,
) -> PersonalContextBootstrapRunRow:
    row = session.get(PersonalContextBootstrapRunRow, run_id)
    if row is None:
        raise PersonalContextBootstrapError("BOOTSTRAP_RUN_NOT_FOUND")
    if row.state != "PAUSED":
        raise PersonalContextBootstrapError(
            "BOOTSTRAP_RUN_NOT_RESUMABLE"
        )
    row.state = "QUEUED"
    row.requested_control = "NONE"
    row.updated_at = _utc(now)
    session.flush()
    return row


def _aggregate_progress(
    current: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    result = dict(current)
    result["batches_completed"] = int(
        result.get("batches_completed", 0)
    ) + 1
    for key, value in metrics.items():
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if key in _STABLE_DISCOVERY_METRICS:
            result[key] = max(int(result.get(key, 0)), value)
        else:
            result[key] = int(result.get(key, 0)) + value
    return result


def _advance_result(
    row: PersonalContextBootstrapRunRow,
    *,
    batch_id: str | None,
    metrics: dict[str, Any] | None = None,
) -> BootstrapAdvanceResult:
    return BootstrapAdvanceResult(
        run_id=row.id,
        state=row.state,
        batch_id=batch_id,
        resume_cursor=row.resume_cursor,
        metrics=dict(metrics or {}),
    )


def process_next_bootstrap_batch(
    session: Session,
    run_id: str,
    *,
    adapter: HistoryAdapter,
    now: datetime | None = None,
) -> BootstrapAdvanceResult:
    """Advance one bounded historical batch and stop at a durable boundary."""
    row = session.get(PersonalContextBootstrapRunRow, run_id)
    if row is None:
        raise PersonalContextBootstrapError("BOOTSTRAP_RUN_NOT_FOUND")

    stamp = _utc(now)
    if row.state in _TERMINAL_STATES or row.state == "PAUSED":
        return _advance_result(row, batch_id=None)

    if row.state == "CREATED":
        raise PersonalContextBootstrapError(
            "BOOTSTRAP_RUN_NOT_QUEUED"
        )

    if row.requested_control == "CANCEL":
        row.state = "CANCELLED"
        row.requested_control = "NONE"
        row.cancelled_at = stamp
        row.updated_at = stamp
        session.flush()
        return _advance_result(row, batch_id=None)
    if row.requested_control == "PAUSE":
        row.state = "PAUSED"
        row.requested_control = "NONE"
        row.paused_at = stamp
        row.updated_at = stamp
        session.flush()
        return _advance_result(row, batch_id=None)

    if row.state not in {"QUEUED", "RUNNING"}:
        raise PersonalContextBootstrapError(
            "BOOTSTRAP_RUN_STATE_INVALID"
        )

    ordinal = (
        session.scalar(
            select(func.max(PersonalContextBootstrapBatchRow.ordinal)).where(
                PersonalContextBootstrapBatchRow.bootstrap_run_id == row.id
            )
        )
        or 0
    ) + 1
    batch_key = (
        "personal-context-bootstrap-batch:"
        + stable_hash({"run_id": row.id, "ordinal": ordinal})[:80]
    )
    batch = PersonalContextBootstrapBatchRow(
        id=new_id(),
        bootstrap_run_id=row.id,
        ordinal=ordinal,
        idempotency_key=batch_key,
        state="RUNNING",
        cursor_before=row.resume_cursor,
        cursor_after=None,
        metrics={},
        failure_summary=None,
        created_at=stamp,
        started_at=stamp,
        completed_at=None,
        updated_at=stamp,
    )
    session.add(batch)
    row.state = "RUNNING"
    row.started_at = row.started_at or stamp
    row.updated_at = stamp
    session.flush()

    budget = _normalized_budget(row.processing_budget)
    selection = _normalized_selection(row.source_selection)
    try:
        with session.begin_nested():
            result = HistoryBackfillService(session, adapter).run(
                chat_keys=selection.get("chat_keys"),
                dry_run=False,
                resume_cursor=row.resume_cursor,
                page_size=budget["page_size"],
                max_messages_per_chat=budget["max_messages_per_chat"],
                max_total_messages=budget["max_total_messages"],
                source_override=_ARCHIVE_SOURCE_BY_KIND[row.source_kind],
                source_account_override=row.source_account,
                tenant_id=row.tenant_id,
            )
    except Exception as exc:
        failed_at = _utc()
        summary = f"{type(exc).__name__}: {exc}"[:500]
        batch.state = "FAILED"
        batch.failure_summary = summary
        batch.completed_at = failed_at
        batch.updated_at = failed_at
        row.state = "FAILED"
        row.failure_summary = summary
        row.failed_at = failed_at
        row.updated_at = failed_at
        session.flush()
        return _advance_result(row, batch_id=batch.id)

    completed_at = _utc()
    batch.state = "COMPLETED"
    batch.cursor_after = result.resume_cursor
    batch.metrics = dict(result.metrics)
    batch.completed_at = completed_at
    batch.updated_at = completed_at

    row.resume_cursor = result.resume_cursor
    row.progress = _aggregate_progress(
        row.progress or {},
        result.metrics,
    )
    row.updated_at = completed_at
    session.flush()

    # A control request made while the bounded batch was running is applied
    # before another batch may start.
    session.refresh(row)
    if row.requested_control == "CANCEL":
        row.state = "CANCELLED"
        row.requested_control = "NONE"
        row.cancelled_at = completed_at
    elif result.resume_cursor is None:
        row.state = "COMPLETED"
        row.requested_control = "NONE"
        row.completed_at = completed_at
    elif row.requested_control == "PAUSE":
        row.state = "PAUSED"
        row.requested_control = "NONE"
        row.paused_at = completed_at
    else:
        row.state = "QUEUED"

    session.flush()
    return _advance_result(
        row,
        batch_id=batch.id,
        metrics=result.metrics,
    )


__all__ = [
    "BOOTSTRAP_MODE",
    "BootstrapAdvanceResult",
    "PersonalContextBootstrapError",
    "SUPPORTED_BOOTSTRAP_SOURCES",
    "create_bootstrap_run",
    "process_next_bootstrap_batch",
    "queue_bootstrap_run",
    "request_bootstrap_control",
    "resolve_represented_owner_actor_key",
    "resume_bootstrap_run",
]
