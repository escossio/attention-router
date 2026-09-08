"""Normalized, bounded reconciliation of one accepted Meta delivery attempt."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id
from attention_router.infrastructure.models import (
    AgentDecisionRow,
    AgentExecutionIntentRow,
    AuditEventRow,
    BoundedRunAuthorizationRow,
    EffectBudgetRow,
    EffectConsumptionRow,
    ExecutionIntentRow,
    ExecutionLeaseRow,
    HumanExecutionAuthorizationRow,
    HumanApprovalDeliveryEvidenceRow,
    InteractionRow,
    MetaCallbackAuditMarkerRow,
    MetaCallbackEvidenceRow,
    MetaCallbackInboxRow,
    MetaDeliveryReconciliationRow,
    OutboxMessageRow,
    ScenarioRunRow,
)
from attention_router.infrastructure.repository import audit
from attention_router.platform.execution_safety import (
    SafetyDenied,
    finalize_consumed_in_transaction,
)
from attention_router.platform.meta_observability import (
    log_sanitized_exception,
    provider_message_id_fingerprint,
)
from attention_router.platform.production_authority import semantic_scope_fingerprint
from attention_router.platform.scenarios import ScenarioRunStatus, transition_scenario_run
from attention_router.platform.transaction_locks import (
    acquire_meta_attempt_gate,
    acquire_meta_provider_gate,
)


logger = logging.getLogger(__name__)

RECONCILIATION_WINDOW_SECONDS = 604800
ADMISSION_GRACE_SECONDS = 30
MAX_ADMISSION_TRANSACTION_SECONDS = 10
MAX_RECONCILIATION_FAILURES = 5
MAX_INBOX_CORRELATION_FAILURES = 5
FAILURE_BACKOFF_BASE_SECONDS = 5
FAILURE_BACKOFF_MAX_SECONDS = 300
MIN_INBOX_CLAIM_SECONDS = 30
META_DESTINATION = "meta_whatsapp_cloud"
META_ACTION_TYPE = "production_conversation_reply"
META_DELIVERY_REASON = "ONE_META_REPLY_DELIVERED"
META_FAILED_REASON = "META_DELIVERY_FAILED"
META_TIMEOUT_REASON = "META_DELIVERY_UNPROVEN_TIMEOUT"
META_INVALID_REASON = "META_DELIVERY_EVIDENCE_INVALID"

META_RECONCILIATION_GLOBAL_LOCK_ORDER = (
    "meta_delivery_reconciliation",
    "outbox",
    "execution_lease",
    "effect_budget",
    "effect_consumption",
    "agent_execution_intent",
    "scenario_run",
    "bounded_run_authorization",
    "human_execution_authorization",
)
META_PRELOCK_GATE_ORDER = (
    "meta_attempt_gate_for_reservation_and_acceptance_only",
    "meta_provider_gate_for_admission_acceptance_and_inbox_correlation_only",
)

_VALID_STATUSES = frozenset({"sent", "delivered", "read", "failed"})
_DELIVERY_STATUSES = frozenset({"delivered", "read"})
_REQUEUE_REASON_CODES = frozenset(
    {
        "OPERATOR_CONFIRMED_RETRY",
        "REVIEWED_SAFE_RETRY",
        "REVIEWED_TRANSIENT_FAILURE",
    }
)
_STRICT_UNIX_SECONDS = re.compile(r"[0-9]+")
_EXISTING_EVIDENCE_UNSET = object()

Decision = Literal["PENDING", "PASSED", "FAILED"]
SessionFactory = Callable[[], Session]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode()).hexdigest()[:48]}"


@dataclass(frozen=True, slots=True)
class MetaCallbackEvidence:
    status: Any
    provider_timestamp_raw: Any
    received_at: datetime
    errors_present: bool = False


@dataclass(frozen=True, slots=True)
class MetaReconciliationDecision:
    decision: Decision
    reason_code: str | None
    evidence_class: str
    latest_valid_provider_timestamp: datetime | None
    delivery_proved: bool
    deadline_at: datetime
    closure_after: datetime
    due: bool
    valid_evidence_count: int
    invalid_evidence_count: int
    late_evidence_count: int
    conflicting_failure_count: int


@dataclass(frozen=True, slots=True)
class MetaAdmissionResult:
    reconciliation_id: str | None
    scope_accepted: bool
    evidence_persisted: bool
    duplicate: bool
    admissible: bool
    inbox_id: str | None = None
    correlation_pending: bool = False


@dataclass(frozen=True, slots=True)
class MetaReconciliationResult:
    reconciliation_id: str
    state_changed: bool
    domain_state_changed: bool
    terminalized: bool
    decision: Decision
    reason_code: str | None
    evidence_class: str
    deadline_at: datetime
    closure_after: datetime


@dataclass(frozen=True, slots=True)
class MetaSweepResult:
    selected: int
    processed: int
    terminalized: int
    failed: int


@dataclass(frozen=True, slots=True)
class MetaInboxSweepResult:
    selected: int
    correlated: int
    deferred: int
    quarantined: int


@dataclass(frozen=True, slots=True)
class _LockedMetaGraph:
    reconciliation: MetaDeliveryReconciliationRow
    outbox: OutboxMessageRow
    lease: ExecutionLeaseRow
    budget: EffectBudgetRow
    consumption: EffectConsumptionRow
    agent: AgentExecutionIntentRow
    run: ScenarioRunRow
    bounded: BoundedRunAuthorizationRow
    authorization: HumanExecutionAuthorizationRow
    parent: ExecutionIntentRow
    decision: AgentDecisionRow
    interaction: InteractionRow


class MetaAdmissionUnavailable(RuntimeError):
    """Evidence was not durably admitted and the provider must retry."""


def parse_provider_timestamp(value: Any) -> datetime | None:
    """Parse strict non-negative integral Unix seconds without fallback."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str) and _STRICT_UNIX_SECONDS.fullmatch(value):
        if len(value) > 12:
            return None
        try:
            seconds = int(value)
        except ValueError:
            return None
    else:
        return None
    if seconds < 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def reduce_meta_callback_evidence(
    evidences: Sequence[MetaCallbackEvidence],
    *,
    accepted_at: datetime,
    now: datetime,
    admission_grace_seconds: int = ADMISSION_GRACE_SECONDS,
) -> MetaReconciliationDecision:
    """Reduce evidence by provider time while using local time for admission."""

    accepted = _utc(accepted_at)
    processing_at = _utc(now)
    deadline_at = accepted + timedelta(seconds=RECONCILIATION_WINDOW_SECONDS)
    closure_after = deadline_at + timedelta(seconds=admission_grace_seconds)
    due = processing_at >= closure_after
    valid: list[tuple[str, datetime]] = []
    invalid_count = 0
    late_count = 0

    for evidence in evidences:
        if _utc(evidence.received_at) > deadline_at:
            late_count += 1
            continue
        provider_timestamp = parse_provider_timestamp(evidence.provider_timestamp_raw)
        if evidence.status not in _VALID_STATUSES or provider_timestamp is None:
            invalid_count += 1
            continue
        valid.append((evidence.status, provider_timestamp))

    latest_timestamp = max((timestamp for _, timestamp in valid), default=None)
    delivery_proved = any(status in _DELIVERY_STATUSES for status, _ in valid)
    failure_count = sum(status == "failed" for status, _ in valid)

    if delivery_proved:
        decision: Decision = "PASSED"
        reason = META_DELIVERY_REASON
        evidence_class = (
            "DELIVERY_PROVED_WITH_FAILED_CONFLICT"
            if failure_count
            else "DELIVERY_PROVED"
        )
    elif not due:
        decision = "PENDING"
        reason = None
        if valid:
            evidence_class = "VALID_NON_DELIVERY_PENDING"
        elif invalid_count:
            evidence_class = "INVALID_ONLY_PENDING"
        else:
            evidence_class = "NO_IN_WINDOW_EVIDENCE_PENDING"
    elif valid:
        latest_statuses = {
            status for status, timestamp in valid if timestamp == latest_timestamp
        }
        if "failed" in latest_statuses:
            decision = "FAILED"
            reason = META_FAILED_REASON
            evidence_class = "PROVIDER_FAILURE"
        else:
            decision = "FAILED"
            reason = META_TIMEOUT_REASON
            evidence_class = "DELIVERY_UNPROVEN"
    elif invalid_count:
        decision = "FAILED"
        reason = META_INVALID_REASON
        evidence_class = "INVALID_ONLY"
    else:
        decision = "FAILED"
        reason = META_TIMEOUT_REASON
        evidence_class = "LATE_ONLY" if late_count else "NO_CALLBACK"

    return MetaReconciliationDecision(
        decision=decision,
        reason_code=reason,
        evidence_class=evidence_class,
        latest_valid_provider_timestamp=latest_timestamp,
        delivery_proved=delivery_proved,
        deadline_at=deadline_at,
        closure_after=closure_after,
        due=due,
        valid_evidence_count=len(valid),
        invalid_evidence_count=invalid_count,
        late_evidence_count=late_count,
        conflicting_failure_count=failure_count if delivery_proved else 0,
    )


def _raw_timestamp_for_storage(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return f"bool:{str(value).lower()}"
    if isinstance(value, int):
        return str(value)[:64]
    if isinstance(value, float):
        return f"float:{value!r}"[:64]
    if isinstance(value, str):
        return value[:64]
    return f"unsupported:{type(value).__name__}"[:64]


def _error_fingerprint(errors: Any) -> str | None:
    if not errors:
        return None
    canonical = json.dumps(errors, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _evidence_deduplication_key(
    *,
    provider_status: str | None,
    provider_timestamp_raw: Any,
    error_fingerprint: str | None,
) -> str:
    value = json.dumps(
        {
            "status": provider_status,
            "timestamp_type": type(provider_timestamp_raw).__name__,
            "timestamp": provider_timestamp_raw,
            "error_fingerprint": error_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _apply_local_timeouts(
    session: Session,
    *,
    transaction_limit_seconds: int,
    reconciliation: bool = False,
) -> None:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    if reconciliation:
        lock_ms = min(3000, transaction_limit_seconds * 1000)
        statement_ms = transaction_limit_seconds * 1000
    else:
        statement_ms = max(250, transaction_limit_seconds * 1000 // 5)
        lock_ms = statement_ms
    session.execute(text(f"SET LOCAL lock_timeout = '{lock_ms}ms'"))
    session.execute(text(f"SET LOCAL statement_timeout = '{statement_ms}ms'"))
    session.execute(text(f"SET LOCAL idle_in_transaction_session_timeout = '{statement_ms}ms'"))


def _status_audit_payload(status_event: dict[str, Any]) -> dict[str, Any]:
    provider_id = status_event.get("id")
    return {
        "provider_message_id_hash": provider_message_id_fingerprint(provider_id),
        "status": status_event.get("status"),
        "timestamp": _raw_timestamp_for_storage(status_event.get("timestamp")),
        "errors_present": bool(status_event.get("errors")),
        "error_fingerprint": _error_fingerprint(status_event.get("errors")),
    }


def persist_human_approval_delivery_evidence(
    session: Session,
    *,
    inbox: MetaCallbackInboxRow,
) -> bool:
    """Link valid delivery evidence to exactly one HEA without audit JSON reads."""

    if not inbox.valid or inbox.provider_status not in _VALID_STATUSES:
        return False
    authorization_ids = session.scalars(
        select(HumanExecutionAuthorizationRow.id).where(
            HumanExecutionAuthorizationRow.request_wamid
            == inbox.provider_message_id,
            HumanExecutionAuthorizationRow.approval_channel
            == "meta_whatsapp_interactive",
        )
    ).all()
    if len(authorization_ids) > 1:
        raise MetaAdmissionUnavailable(
            "META_HUMAN_APPROVAL_PROVIDER_CORRELATION_AMBIGUOUS"
        )
    if not authorization_ids:
        return False
    existing = session.scalar(
        select(HumanApprovalDeliveryEvidenceRow).where(
            HumanApprovalDeliveryEvidenceRow.inbox_id == inbox.id
        )
    )
    if existing is not None:
        if (
            existing.authorization_id != authorization_ids[0]
            or existing.deduplication_key != inbox.deduplication_key
            or existing.provider_status != inbox.provider_status
            or existing.received_at != inbox.received_at
        ):
            raise MetaAdmissionUnavailable(
                "META_HUMAN_APPROVAL_DELIVERY_EVIDENCE_CONFLICT"
            )
        return False
    session.add(
        HumanApprovalDeliveryEvidenceRow(
            id=_stable_id("headel", f"inbox:{inbox.id}"),
            authorization_id=authorization_ids[0],
            inbox_id=inbox.id,
            source_audit_id=None,
            deduplication_key=inbox.deduplication_key,
            provider_status=inbox.provider_status,
            received_at=inbox.received_at,
            created_at=inbox.created_at,
        )
    )
    session.flush()
    return True


def _basic_outbox_scope_valid(
    reconciliation: MetaDeliveryReconciliationRow,
    outbox: OutboxMessageRow,
    *,
    allow_unbound_provider: bool = False,
) -> bool:
    stored_provider = outbox.payload.get("provider_message_id")
    return (
        reconciliation.outbox_message_id == outbox.id
        and (
            reconciliation.provider_message_id == stored_provider
            or (allow_unbound_provider and not stored_provider)
        )
        and outbox.destination == META_DESTINATION
        and outbox.action_type == META_ACTION_TYPE
        and outbox.attempt_count == 1
        and outbox.payload.get("retry_policy") == "NONE"
        and bool(outbox.execution_intent_id)
    )


def _integrity_constraint_name(exc: IntegrityError) -> str | None:
    original = getattr(exc, "orig", None)
    diagnostic = getattr(original, "diag", None)
    return getattr(diagnostic, "constraint_name", None)


def _is_exact_inbox_duplicate(exc: IntegrityError) -> bool:
    return (
        _integrity_constraint_name(exc)
        == "uq_meta_callback_inbox_provider_deduplication"
    )


def _admit_meta_callback_inbox_in_transaction(
    session: Session,
    *,
    status_event: dict[str, Any],
    received_at: datetime,
    processing_at: datetime,
    max_transaction_seconds: int,
) -> MetaAdmissionResult:
    provider_message_id = status_event.get("id")
    provider_message_id = provider_message_id if isinstance(provider_message_id, str) else ""
    if not provider_message_id or len(provider_message_id) > 255:
        raise MetaAdmissionUnavailable("META_CALLBACK_PROVIDER_ID_REQUIRED")
    acquire_meta_provider_gate(session, provider_message_id)
    stored_window = session.execute(
        select(
            MetaDeliveryReconciliationRow.deadline_at,
            MetaDeliveryReconciliationRow.closure_after,
        ).where(
            MetaDeliveryReconciliationRow.provider_message_id == provider_message_id
        )
    ).one_or_none()
    if stored_window is not None:
        stored_grace_seconds = int(
            (_utc(stored_window.closure_after) - _utc(stored_window.deadline_at))
            .total_seconds()
        )
        if not 0 < max_transaction_seconds < stored_grace_seconds:
            raise MetaAdmissionUnavailable(
                "META_CALLBACK_ADMISSION_TIMEOUT_EXCEEDS_STORED_GRACE"
            )
        if session.scalar(
            select(HumanExecutionAuthorizationRow.id).where(
                HumanExecutionAuthorizationRow.request_wamid
                == provider_message_id
            )
        ) is not None:
            raise MetaAdmissionUnavailable(
                "META_PROVIDER_SCOPE_COLLISION"
            )

    provider_status = status_event.get("status")
    provider_status = provider_status if isinstance(provider_status, str) else None
    provider_timestamp_raw = status_event.get("timestamp")
    provider_timestamp = parse_provider_timestamp(provider_timestamp_raw)
    valid = provider_status in _VALID_STATUSES and provider_timestamp is not None
    error_fingerprint = _error_fingerprint(status_event.get("errors"))
    deduplication_key = _evidence_deduplication_key(
        provider_status=provider_status,
        provider_timestamp_raw=provider_timestamp_raw,
        error_fingerprint=error_fingerprint,
    )
    existing = session.scalar(
        select(MetaCallbackInboxRow).where(
            MetaCallbackInboxRow.provider_message_id == provider_message_id,
            MetaCallbackInboxRow.deduplication_key == deduplication_key,
        )
    )
    if existing is not None:
        persist_human_approval_delivery_evidence(session, inbox=existing)
        return MetaAdmissionResult(
            existing.reconciliation_id,
            existing.state == "CORRELATED",
            False,
            True,
            False,
            existing.id,
            existing.state == "PENDING",
        )

    inbox = MetaCallbackInboxRow(
        id=new_id(),
        provider_message_id=provider_message_id,
        deduplication_key=deduplication_key,
        provider_status=provider_status[:32] if provider_status else None,
        provider_timestamp_raw=_raw_timestamp_for_storage(provider_timestamp_raw),
        provider_timestamp=provider_timestamp,
        received_at=_utc(received_at),
        valid=valid,
        errors_present=bool(status_event.get("errors")),
        error_fingerprint=error_fingerprint,
        state="PENDING",
        reconciliation_id=None,
        correlation_attempt_count=0,
        next_correlation_at=_utc(processing_at),
        last_error_code=None,
        correlated_at=None,
        quarantined_at=None,
        claim_token=None,
        claimed_by=None,
        claimed_at=None,
        created_at=_utc(processing_at),
        updated_at=_utc(processing_at),
    )
    duplicate_existing: MetaCallbackInboxRow | None = None
    try:
        with session.begin_nested():
            session.add(inbox)
            session.flush()
    except IntegrityError as exc:
        if not _is_exact_inbox_duplicate(exc):
            raise
        existing = session.scalar(
            select(MetaCallbackInboxRow).where(
                MetaCallbackInboxRow.provider_message_id == provider_message_id,
                MetaCallbackInboxRow.deduplication_key == deduplication_key,
            )
        )
        if existing is None:
            raise
        duplicate_existing = existing
    if duplicate_existing is not None:
        persist_human_approval_delivery_evidence(
            session, inbox=duplicate_existing
        )
        return MetaAdmissionResult(
            duplicate_existing.reconciliation_id,
            duplicate_existing.state == "CORRELATED",
            False,
            True,
            False,
            duplicate_existing.id,
            duplicate_existing.state == "PENDING",
        )
    persist_human_approval_delivery_evidence(session, inbox=inbox)
    audit(
        session,
        None,
        "meta_status_received",
        _status_audit_payload(status_event),
        origin="ingress",
        created_at=_utc(received_at),
    )
    session.flush()
    return MetaAdmissionResult(None, False, True, False, False, inbox.id, True)


def _correlate_pending_inbox_locked(
    session: Session,
    *,
    reconciliation: MetaDeliveryReconciliationRow,
    inbox: MetaCallbackInboxRow,
    now: datetime,
    allow_exact_acceptance_requeue: bool = False,
    existing_evidence: MetaCallbackEvidenceRow | None | object = (
        _EXISTING_EVIDENCE_UNSET
    ),
    outbox: OutboxMessageRow | None = None,
    flush: bool = True,
    stage_only: bool = False,
    prerequisites_flushed: bool = False,
) -> bool:
    if inbox.state == "CORRELATED":
        return False
    exact_acceptance_requeue = (
        allow_exact_acceptance_requeue
        and inbox.state == "QUARANTINED"
        and inbox.last_error_code == "PRODUCTION_META_RECONCILIATION_NOT_FOUND"
    )
    if (
        inbox.state != "PENDING"
        and not exact_acceptance_requeue
    ) or inbox.provider_message_id != reconciliation.provider_message_id:
        raise SafetyDenied("PRODUCTION_META_INBOX_CORRELATION_INVALID")
    if outbox is None:
        outbox = session.get(OutboxMessageRow, reconciliation.outbox_message_id)
    if outbox is None or not _basic_outbox_scope_valid(
        reconciliation, outbox, allow_unbound_provider=True
    ):
        raise SafetyDenied("PRODUCTION_META_OUTBOX_SCOPE_INVALID")
    existing = existing_evidence
    if existing is _EXISTING_EVIDENCE_UNSET:
        existing = session.scalar(
            select(MetaCallbackEvidenceRow).where(
                MetaCallbackEvidenceRow.inbox_id == inbox.id
            )
        )
        if existing is None:
            existing = session.scalar(
                select(MetaCallbackEvidenceRow).where(
                    MetaCallbackEvidenceRow.reconciliation_id == reconciliation.id,
                    MetaCallbackEvidenceRow.deduplication_key
                    == inbox.deduplication_key,
                )
            )
    assert existing is None or isinstance(existing, MetaCallbackEvidenceRow)
    if existing is not None and (
        existing.reconciliation_id != reconciliation.id
        or existing.inbox_id not in {None, inbox.id}
        or existing.deduplication_key != inbox.deduplication_key
        or existing.provider_status != inbox.provider_status
        or existing.provider_timestamp_raw != inbox.provider_timestamp_raw
        or existing.provider_timestamp != inbox.provider_timestamp
        or existing.valid != inbox.valid
        or existing.errors_present != inbox.errors_present
        or existing.error_fingerprint != inbox.error_fingerprint
    ):
        raise SafetyDenied("PRODUCTION_META_EVIDENCE_IDENTITY_CONFLICT")
    evidence_inserted = existing is None
    if evidence_inserted:
        session.add(
            MetaCallbackEvidenceRow(
                id=_stable_id("metaevidence", inbox.id),
                reconciliation_id=reconciliation.id,
                inbox_id=inbox.id,
                deduplication_key=inbox.deduplication_key,
                provider_status=inbox.provider_status,
                provider_timestamp_raw=inbox.provider_timestamp_raw,
                provider_timestamp=inbox.provider_timestamp,
                received_at=inbox.received_at,
                valid=inbox.valid,
                admissible=_utc(inbox.received_at) <= _utc(reconciliation.deadline_at),
                errors_present=inbox.errors_present,
                error_fingerprint=inbox.error_fingerprint,
                created_at=_utc(now),
            )
        )
    if exact_acceptance_requeue and not prerequisites_flushed:
        audit(
            session,
            None,
            "production_meta_callback_exact_acceptance_requeued",
            {
                "inbox_id": inbox.id,
                "reconciliation_id": reconciliation.id,
                "scope": "API_ACCEPTANCE",
                "reason_code": "EXACT_RECONCILIATION_CREATED",
            },
            origin="meta_callback_reconciler_v2",
            tenant_id=reconciliation.tenant_id,
            created_at=_utc(now),
            previous_state="QUARANTINED",
            next_state="CORRELATED",
        )
    if stage_only:
        if not exact_acceptance_requeue:
            raise SafetyDenied("PRODUCTION_META_INBOX_STAGING_INVALID")
        return evidence_inserted
    if exact_acceptance_requeue and not prerequisites_flushed:
        # The database guard requires both durable evidence and the explicit
        # recovery audit to exist before the quarantined row can transition.
        session.flush()
    inbox.state = "CORRELATED"
    inbox.reconciliation_id = reconciliation.id
    inbox.correlated_at = _utc(now)
    inbox.updated_at = _utc(now)
    inbox.last_error_code = None
    inbox.quarantined_at = None
    inbox.claim_token = None
    inbox.claimed_by = None
    inbox.claimed_at = None
    if flush:
        session.flush()
    return evidence_inserted


def _preload_existing_evidence_for_inbox_batch(
    session: Session,
    *,
    reconciliation_id: str,
    inbox_rows: Sequence[MetaCallbackInboxRow],
) -> dict[str, MetaCallbackEvidenceRow | None]:
    if not inbox_rows:
        return {}
    inbox_ids = [row.id for row in inbox_rows]
    deduplication_keys = [row.deduplication_key for row in inbox_rows]
    evidences = session.scalars(
        select(MetaCallbackEvidenceRow).where(
            or_(
                MetaCallbackEvidenceRow.inbox_id.in_(inbox_ids),
                and_(
                    MetaCallbackEvidenceRow.reconciliation_id
                    == reconciliation_id,
                    MetaCallbackEvidenceRow.deduplication_key.in_(
                        deduplication_keys
                    ),
                ),
            )
        )
    ).all()
    by_inbox = {
        evidence.inbox_id: evidence
        for evidence in evidences
        if evidence.inbox_id is not None
    }
    by_deduplication = {
        evidence.deduplication_key: evidence
        for evidence in evidences
        if evidence.reconciliation_id == reconciliation_id
    }
    result: dict[str, MetaCallbackEvidenceRow | None] = {}
    for inbox in inbox_rows:
        candidates = {
            evidence.id: evidence
            for evidence in (
                by_inbox.get(inbox.id),
                by_deduplication.get(inbox.deduplication_key),
            )
            if evidence is not None
        }
        if len(candidates) > 1:
            raise SafetyDenied("PRODUCTION_META_EVIDENCE_IDENTITY_CONFLICT")
        result[inbox.id] = next(iter(candidates.values()), None)
    return result


def _correlate_provider_inbox_in_transaction(
    session: Session,
    *,
    provider_message_id: str,
    now: datetime,
    inbox_id: str | None = None,
    allow_exact_acceptance_requeue: bool = False,
) -> tuple[str | None, bool, bool]:
    acquire_meta_provider_gate(session, provider_message_id)
    reconciliation = session.scalar(
        select(MetaDeliveryReconciliationRow)
        .where(MetaDeliveryReconciliationRow.provider_message_id == provider_message_id)
        .with_for_update(key_share=True)
    )
    if reconciliation is None:
        return None, False, False
    states = ["PENDING"]
    if allow_exact_acceptance_requeue:
        states.append("QUARANTINED")
    query = select(MetaCallbackInboxRow).where(
        MetaCallbackInboxRow.provider_message_id == provider_message_id,
        MetaCallbackInboxRow.state.in_(states),
    )
    if allow_exact_acceptance_requeue:
        query = query.where(
            (MetaCallbackInboxRow.state == "PENDING")
            | (
                (MetaCallbackInboxRow.state == "QUARANTINED")
                & (
                    MetaCallbackInboxRow.last_error_code
                    == "PRODUCTION_META_RECONCILIATION_NOT_FOUND"
                )
            )
        )
    if inbox_id is not None:
        query = query.where(MetaCallbackInboxRow.id == inbox_id)
    inbox_rows = session.scalars(
        query.order_by(MetaCallbackInboxRow.received_at, MetaCallbackInboxRow.id)
        .limit(100)
        .with_for_update()
    ).all()
    existing_evidence = _preload_existing_evidence_for_inbox_batch(
        session,
        reconciliation_id=reconciliation.id,
        inbox_rows=inbox_rows,
    )
    outbox = session.get(OutboxMessageRow, reconciliation.outbox_message_id)
    inserted = False
    admissible = False
    exact_recoveries = [
        inbox
        for inbox in inbox_rows
        if inbox.state == "QUARANTINED"
        and inbox.last_error_code == "PRODUCTION_META_RECONCILIATION_NOT_FOUND"
    ]
    for inbox in exact_recoveries:
        inserted |= _correlate_pending_inbox_locked(
            session,
            reconciliation=reconciliation,
            inbox=inbox,
            now=now,
            allow_exact_acceptance_requeue=True,
            existing_evidence=existing_evidence[inbox.id],
            outbox=outbox,
            flush=False,
            stage_only=True,
        )
    if exact_recoveries:
        session.flush()
        existing_evidence = _preload_existing_evidence_for_inbox_batch(
            session,
            reconciliation_id=reconciliation.id,
            inbox_rows=inbox_rows,
        )
    for inbox in inbox_rows:
        try:
            inserted |= _correlate_pending_inbox_locked(
                session,
                reconciliation=reconciliation,
                inbox=inbox,
                now=now,
                allow_exact_acceptance_requeue=allow_exact_acceptance_requeue,
                existing_evidence=existing_evidence[inbox.id],
                outbox=outbox,
                flush=False,
                prerequisites_flushed=inbox in exact_recoveries,
            )
        except SafetyDenied as exc:
            inbox.state = "QUARANTINED"
            inbox.quarantined_at = _utc(now)
            inbox.updated_at = _utc(now)
            inbox.last_error_code = str(exc)[:120]
            inbox.claim_token = None
            inbox.claimed_by = None
            inbox.claimed_at = None
            audit(
                session,
                None,
                "production_meta_callback_scope_rejected",
                {
                    "inbox_id": inbox.id,
                    "reason_code": inbox.last_error_code,
                },
                origin="meta_callback_admission",
                tenant_id=reconciliation.tenant_id,
                created_at=_utc(now),
            )
            session.flush()
            return reconciliation.id, False, False
        admissible |= _utc(inbox.received_at) <= _utc(reconciliation.deadline_at)
    session.flush()
    return reconciliation.id, bool(inbox_rows), admissible


def _correlate_pending_inbox_for_locked_reconciliation(
    session: Session,
    reconciliation: MetaDeliveryReconciliationRow,
    *,
    now: datetime,
) -> int:
    rows = session.scalars(
        select(MetaCallbackInboxRow)
        .where(
            MetaCallbackInboxRow.provider_message_id
            == reconciliation.provider_message_id,
            (MetaCallbackInboxRow.state == "PENDING")
            | (
                (MetaCallbackInboxRow.state == "QUARANTINED")
                & (
                    MetaCallbackInboxRow.last_error_code
                    == "PRODUCTION_META_RECONCILIATION_NOT_FOUND"
                )
            ),
        )
        .order_by(MetaCallbackInboxRow.received_at, MetaCallbackInboxRow.id)
        .limit(100)
        .with_for_update()
    ).all()
    existing_evidence = _preload_existing_evidence_for_inbox_batch(
        session,
        reconciliation_id=reconciliation.id,
        inbox_rows=rows,
    )
    outbox = session.get(OutboxMessageRow, reconciliation.outbox_message_id)
    exact_recoveries = [
        inbox
        for inbox in rows
        if inbox.state == "QUARANTINED"
        and inbox.last_error_code == "PRODUCTION_META_RECONCILIATION_NOT_FOUND"
    ]
    correlated = 0
    for inbox in exact_recoveries:
        correlated += int(
            _correlate_pending_inbox_locked(
                session,
                reconciliation=reconciliation,
                inbox=inbox,
                now=now,
                allow_exact_acceptance_requeue=True,
                existing_evidence=existing_evidence[inbox.id],
                outbox=outbox,
                flush=False,
                stage_only=True,
            )
        )
    if exact_recoveries:
        session.flush()
        existing_evidence = _preload_existing_evidence_for_inbox_batch(
            session,
            reconciliation_id=reconciliation.id,
            inbox_rows=rows,
        )
    for inbox in rows:
        newly_inserted = _correlate_pending_inbox_locked(
            session,
            reconciliation=reconciliation,
            inbox=inbox,
            now=now,
            allow_exact_acceptance_requeue=True,
            existing_evidence=existing_evidence[inbox.id],
            outbox=outbox,
            flush=False,
            prerequisites_flushed=inbox in exact_recoveries,
        )
        if inbox not in exact_recoveries:
            correlated += int(newly_inserted)
    session.flush()
    return correlated


def admit_meta_callback_evidence(
    session_factory: SessionFactory,
    *,
    status_event: dict[str, Any],
    received_at: datetime,
    max_transaction_seconds: int = MAX_ADMISSION_TRANSACTION_SECONDS,
    admission_grace_seconds: int = ADMISSION_GRACE_SECONDS,
) -> MetaAdmissionResult:
    """Persist one evidence item before attempting any graph lock."""

    if not 0 < max_transaction_seconds < admission_grace_seconds:
        raise ValueError("admission transaction timeout must be inside grace")
    started = time.monotonic()
    admission_failed = False
    try:
        with session_factory() as session, session.begin():
            _apply_local_timeouts(
                session, transaction_limit_seconds=max_transaction_seconds
            )
            admitted = _admit_meta_callback_inbox_in_transaction(
                session,
                status_event=status_event,
                received_at=_utc(received_at),
                processing_at=datetime.now(UTC),
                max_transaction_seconds=max_transaction_seconds,
            )
            if time.monotonic() - started >= max_transaction_seconds:
                raise TimeoutError("Meta admission transaction exceeded its limit")
    except (DBAPIError, IntegrityError, TimeoutError):
        admission_failed = True
    if admission_failed:
        raise MetaAdmissionUnavailable(
            "META_CALLBACK_ADMISSION_RETRY_REQUIRED"
        ) from None
    if admitted.duplicate and not admitted.correlation_pending:
        return admitted
    provider_message_id = status_event.get("id")
    assert isinstance(provider_message_id, str)
    try:
        with session_factory() as session, session.begin():
            _apply_local_timeouts(
                session, transaction_limit_seconds=max_transaction_seconds
            )
            reconciliation_id, scope_accepted, admissible = (
                _correlate_provider_inbox_in_transaction(
                    session,
                    provider_message_id=provider_message_id,
                    now=datetime.now(UTC),
                    inbox_id=admitted.inbox_id,
                )
            )
    except Exception as exc:
        log_sanitized_exception(
            logger,
            "durable Meta callback inbox correlation deferred inbox_id=%s",
            admitted.inbox_id,
            exc=exc,
        )
        return admitted
    return MetaAdmissionResult(
        reconciliation_id,
        scope_accepted,
        admitted.evidence_persisted,
        admitted.duplicate,
        admissible,
        admitted.inbox_id,
        reconciliation_id is None,
    )


def _operational_label(value: str, *, field: str, max_length: int = 120) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if (
        not normalized
        or len(normalized) > max_length
        or re.fullmatch(r"[A-Za-z0-9_.:@-]+", normalized) is None
    ):
        raise ValueError(f"{field} must be a bounded operational identifier")
    return normalized


def _requeue_reason(value: str) -> str:
    reason = _operational_label(value, field="reason", max_length=80)
    if reason not in _REQUEUE_REASON_CODES:
        raise ValueError("reason must be an approved requeue reason code")
    return reason


def _claim_pending_meta_callback_inbox(
    session_factory: SessionFactory,
    *,
    worker_id: str,
    now: datetime,
    excluded: set[str],
    transaction_timeout_seconds: int,
) -> tuple[str, str, str] | None:
    claim_seconds = max(
        MIN_INBOX_CLAIM_SECONDS,
        transaction_timeout_seconds * 2,
    )
    with session_factory() as session, session.begin():
        _apply_local_timeouts(
            session,
            transaction_limit_seconds=transaction_timeout_seconds,
        )
        query = select(MetaCallbackInboxRow).where(
            MetaCallbackInboxRow.state == "PENDING",
            MetaCallbackInboxRow.next_correlation_at <= now,
        )
        if excluded:
            query = query.where(MetaCallbackInboxRow.id.not_in(excluded))
        inbox = session.scalar(
            query.order_by(
                MetaCallbackInboxRow.next_correlation_at,
                MetaCallbackInboxRow.id,
            )
            .limit(1)
            .with_for_update(skip_locked=True, key_share=True)
        )
        if inbox is None:
            return None
        claim_token = new_id()
        inbox.claim_token = claim_token
        inbox.claimed_by = worker_id
        inbox.claimed_at = now
        inbox.next_correlation_at = now + timedelta(seconds=claim_seconds)
        inbox.updated_at = now
        session.flush()
        return inbox.id, inbox.provider_message_id, claim_token


def _record_inbox_processing_failure(
    session_factory: SessionFactory,
    *,
    inbox_id: str,
    claim_token: str,
    worker_id: str,
    error_type: str,
    now: datetime,
) -> bool:
    try:
        with session_factory() as session, session.begin():
            inbox = session.scalar(
                select(MetaCallbackInboxRow)
                .where(
                    MetaCallbackInboxRow.id == inbox_id,
                    MetaCallbackInboxRow.state == "PENDING",
                    MetaCallbackInboxRow.claim_token == claim_token,
                )
                .with_for_update(key_share=True)
            )
            if inbox is None:
                return False
            attempt = inbox.correlation_attempt_count + 1
            inbox.correlation_attempt_count = attempt
            inbox.last_error_code = error_type[:120]
            inbox.updated_at = now
            inbox.claim_token = None
            inbox.claimed_by = None
            inbox.claimed_at = None
            quarantined = attempt >= MAX_INBOX_CORRELATION_FAILURES
            if quarantined:
                inbox.state = "QUARANTINED"
                inbox.quarantined_at = now
            else:
                delay = min(
                    FAILURE_BACKOFF_MAX_SECONDS,
                    FAILURE_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)),
                )
                inbox.next_correlation_at = now + timedelta(seconds=delay)
            audit(
                session,
                None,
                "production_meta_callback_correlation_failed",
                {
                    "inbox_id": inbox.id,
                    "worker_id": worker_id,
                    "attempt_count": attempt,
                    "state": inbox.state,
                    "error_type": inbox.last_error_code,
                },
                origin="meta_callback_reconciler_v2",
                created_at=now,
            )
            session.flush()
            return quarantined
    except Exception as persist_exc:
        log_sanitized_exception(
            logger,
            "failed to persist Meta callback inbox failure inbox_id=%s",
            inbox_id,
            exc=persist_exc,
        )
        return False


def reconcile_pending_meta_callback_inbox(
    session_factory: SessionFactory,
    *,
    worker_id: str,
    now: datetime | None = None,
    limit: int = 100,
    transaction_timeout_seconds: int = 10,
) -> MetaInboxSweepResult:
    """Bounded DB-only correlation of durable pre-acceptance receipts."""

    if not 0 < limit <= 100:
        raise ValueError("Meta callback inbox batch limit must be between 1 and 100")
    if transaction_timeout_seconds <= 0:
        raise ValueError("Meta callback inbox transaction timeout must be positive")
    worker_id = _operational_label(worker_id, field="worker_id")
    timestamp = _utc(now or datetime.now(UTC))
    excluded: set[str] = set()
    selected = correlated = deferred = quarantined = 0
    while selected < limit:
        claim = _claim_pending_meta_callback_inbox(
            session_factory,
            worker_id=worker_id,
            now=timestamp,
            excluded=excluded,
            transaction_timeout_seconds=transaction_timeout_seconds,
        )
        if claim is None:
            break
        inbox_id, provider_message_id, claim_token = claim
        excluded.add(inbox_id)
        selected += 1
        try:
            with session_factory() as session, session.begin():
                _apply_local_timeouts(
                    session, transaction_limit_seconds=transaction_timeout_seconds
                )
                acquire_meta_provider_gate(session, provider_message_id)
                reconciliation = session.scalar(
                    select(MetaDeliveryReconciliationRow)
                    .where(
                        MetaDeliveryReconciliationRow.provider_message_id
                        == provider_message_id
                    )
                    .with_for_update(key_share=True)
                )
                inbox = session.execute(
                    select(MetaCallbackInboxRow)
                    .where(
                        MetaCallbackInboxRow.id == inbox_id,
                        MetaCallbackInboxRow.state == "PENDING",
                        MetaCallbackInboxRow.claim_token == claim_token,
                    )
                    .with_for_update(skip_locked=True)
                ).scalar_one_or_none()
                if inbox is None:
                    continue
                if reconciliation is not None:
                    try:
                        _correlate_pending_inbox_locked(
                            session,
                            reconciliation=reconciliation,
                            inbox=inbox,
                            now=timestamp,
                        )
                        correlated += 1
                    except SafetyDenied as exc:
                        inbox.state = "QUARANTINED"
                        inbox.quarantined_at = timestamp
                        inbox.updated_at = timestamp
                        inbox.last_error_code = str(exc)[:120]
                        inbox.claim_token = None
                        inbox.claimed_by = None
                        inbox.claimed_at = None
                        quarantined += 1
                        audit(
                            session,
                            None,
                            "production_meta_callback_scope_rejected",
                            {
                                "inbox_id": inbox.id,
                                "reason_code": inbox.last_error_code,
                            },
                            origin="meta_callback_reconciler_v2",
                            tenant_id=reconciliation.tenant_id,
                            created_at=timestamp,
                        )
                        session.flush()
                    continue
                attempt = inbox.correlation_attempt_count + 1
                inbox.correlation_attempt_count = attempt
                inbox.last_error_code = "PRODUCTION_META_RECONCILIATION_NOT_FOUND"
                inbox.updated_at = timestamp
                inbox.claim_token = None
                inbox.claimed_by = None
                inbox.claimed_at = None
                if attempt >= MAX_INBOX_CORRELATION_FAILURES:
                    inbox.state = "QUARANTINED"
                    inbox.quarantined_at = timestamp
                    quarantined += 1
                else:
                    delay = min(
                        FAILURE_BACKOFF_MAX_SECONDS,
                        FAILURE_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)),
                    )
                    inbox.next_correlation_at = timestamp + timedelta(seconds=delay)
                    deferred += 1
                audit(
                    session,
                    None,
                    "production_meta_callback_correlation_deferred",
                    {
                        "inbox_id": inbox.id,
                        "worker_id": worker_id,
                        "attempt_count": attempt,
                        "state": inbox.state,
                        "reason_code": inbox.last_error_code,
                    },
                    origin="meta_callback_reconciler_v2",
                    created_at=timestamp,
                )
                session.flush()
        except Exception as exc:
            was_quarantined = _record_inbox_processing_failure(
                session_factory,
                inbox_id=inbox_id,
                claim_token=claim_token,
                worker_id=worker_id,
                error_type=type(exc).__name__,
                now=timestamp,
            )
            quarantined += int(was_quarantined)
            log_sanitized_exception(
                logger,
                "Meta callback inbox row failed inbox_id=%s worker_id=%s",
                inbox_id,
                worker_id,
                exc=exc,
            )
            deferred += 1
    return MetaInboxSweepResult(selected, correlated, deferred, quarantined)


def _locked_one(session: Session, model: type[Any], identifier: str | None) -> Any | None:
    if not identifier:
        return None
    return session.execute(
        select(model).where(model.id == identifier).with_for_update()
    ).scalar_one_or_none()


def _lock_meta_graph(
    session: Session,
    reconciliation: MetaDeliveryReconciliationRow,
    *,
    accepting: bool = False,
) -> _LockedMetaGraph:
    """Acquire the only supported operational graph lock order."""

    reconciliation = session.execute(
        select(MetaDeliveryReconciliationRow)
        .where(MetaDeliveryReconciliationRow.id == reconciliation.id)
        .with_for_update(key_share=True)
    ).scalar_one()
    outbox = _locked_one(session, OutboxMessageRow, reconciliation.outbox_message_id)
    if outbox is None or not _basic_outbox_scope_valid(
        reconciliation,
        outbox,
        allow_unbound_provider=accepting,
    ):
        raise SafetyDenied("PRODUCTION_META_OUTBOX_SCOPE_INVALID")
    consumption_snapshot = session.scalar(
        select(EffectConsumptionRow).where(
            EffectConsumptionRow.outbox_message_id == outbox.id
        )
    )
    if consumption_snapshot is None:
        raise SafetyDenied("PRODUCTION_META_GRAPH_CONSUMPTION_MISSING")

    lease = _locked_one(session, ExecutionLeaseRow, consumption_snapshot.execution_lease_id)
    budget = _locked_one(session, EffectBudgetRow, consumption_snapshot.effect_budget_id)
    consumption = _locked_one(session, EffectConsumptionRow, consumption_snapshot.id)
    agent = _locked_one(session, AgentExecutionIntentRow, outbox.execution_intent_id)
    run = session.execute(
        select(ScenarioRunRow)
        .where(ScenarioRunRow.agent_execution_intent_id == outbox.execution_intent_id)
        .with_for_update()
    ).scalar_one_or_none()
    bounded = session.execute(
        select(BoundedRunAuthorizationRow)
        .where(
            BoundedRunAuthorizationRow.scenario_run_id == run.id,
            BoundedRunAuthorizationRow.effect_budget_id == budget.id,
        )
        .with_for_update()
    ).scalar_one_or_none() if run is not None and budget is not None else None
    authorization = _locked_one(
        session, HumanExecutionAuthorizationRow, outbox.causation_id
    )
    parent = session.get(
        ExecutionIntentRow, agent.execution_intent_id if agent is not None else None
    )
    decision = session.get(
        AgentDecisionRow, agent.agent_decision_id if agent is not None else None
    )
    interaction = session.get(
        InteractionRow, decision.interaction_id if decision is not None else None
    )

    values = (
        lease,
        budget,
        consumption,
        agent,
        run,
        bounded,
        authorization,
        parent,
        decision,
        interaction,
    )
    if any(value is None for value in values):
        raise SafetyDenied("PRODUCTION_META_GRAPH_INCOMPLETE")
    assert lease is not None
    assert budget is not None
    assert consumption is not None
    assert agent is not None
    assert run is not None
    assert bounded is not None
    assert authorization is not None
    assert parent is not None
    assert decision is not None
    assert interaction is not None

    tenant = interaction.tenant_id
    expected_effect = f"execution:{agent.id}"
    expected_target = agent.recipient_reference or ""
    parent_scope = parent.scope if isinstance(parent.scope, dict) else {}
    raw_parent_target = parent_scope.get("target")
    raw_frozen_authority = parent_scope.get("frozen_authority")
    parent_target = raw_parent_target if isinstance(raw_parent_target, dict) else {}
    frozen_authority = (
        raw_frozen_authority if isinstance(raw_frozen_authority, dict) else {}
    )
    if (
        reconciliation.tenant_id != tenant
        or outbox.interaction_id != interaction.id
        or outbox.execution_intent_id != agent.id
        or outbox.idempotency_key != expected_effect
        or outbox.correlation_id != run.root_correlation_id
        or outbox.causation_id != authorization.id
        or agent.authorization_source != "PRODUCTION_EXECUTION_INTENT"
        or agent.capability_name != "conversation.reply"
        or not agent.execution_intent_id
        or parent.id != agent.execution_intent_id
        or parent.state != "MATERIALIZED"
        or parent.scope_fingerprint != agent.execution_intent_fingerprint
        or authorization.execution_intent_id != parent.id
        or authorization.execution_intent_fingerprint != parent.scope_fingerprint
        or decision.id != agent.agent_decision_id
        or decision.interaction_id != interaction.id
        or run.agent_execution_intent_id != agent.id
        or run.effect_budget_id != budget.id
        or budget.scenario_run_id != run.id
        or lease.scenario_run_id != run.id
        or lease.effect_budget_id != budget.id
        or lease.logical_execution_id != agent.idempotency_key
        or lease.claimant_id != agent.id
        or consumption.effect_budget_id != budget.id
        or consumption.execution_lease_id != lease.id
        or consumption.execution_intent_id != agent.id
        or consumption.outbox_message_id != outbox.id
        or consumption.logical_effect_id != expected_effect
        or consumption.idempotency_key != expected_effect
        or consumption.target_scope != expected_target
        or bounded.scenario_run_id != run.id
        or bounded.effect_budget_id != budget.id
        or bounded.actor_scope != f"human-approval:{authorization.id}"
        or bounded.target_scope != expected_target
        or bounded.capability_scope != "conversation.reply"
        or bounded.effect_scope != "WHATSAPP_TEXT"
        or bounded.max_effects != 1
        or semantic_scope_fingerprint(parent_scope) != parent.scope_fingerprint
        or parent_target.get("tenant") != tenant
        or parent_target.get("transport") != "meta_whatsapp"
        or frozen_authority.get("transport") != "meta_whatsapp"
        or frozen_authority.get("operation") != "conversation.reply"
        or frozen_authority.get("capability") != "conversation.reply"
        or frozen_authority.get("outbound_messages") != 1
        or frozen_authority.get("action_count") != 1
        or frozen_authority.get("retries") != 0
        or {
            run.tenant_id,
            budget.tenant_id,
            lease.tenant_id,
            consumption.tenant_id,
            bounded.tenant_id,
        }
        != {tenant}
    ):
        raise SafetyDenied("PRODUCTION_META_GRAPH_SCOPE_MISMATCH")

    return _LockedMetaGraph(
        reconciliation=reconciliation,
        outbox=outbox,
        lease=lease,
        budget=budget,
        consumption=consumption,
        agent=agent,
        run=run,
        bounded=bounded,
        authorization=authorization,
        parent=parent,
        decision=decision,
        interaction=interaction,
    )


def _authority_is_reserved(graph: _LockedMetaGraph) -> bool:
    return (
        graph.authorization.state == "APPROVED"
        and graph.bounded.status == "ACTIVE"
        and graph.lease.status == "CLAIMED"
        and graph.consumption.state == "RESERVED"
        and graph.budget.status == "RESERVED"
        and graph.budget.reserved_count == 1
        and graph.budget.consumed_count == 0
        and graph.agent.status == "QUEUED"
        and graph.run.status == "RUNNING"
    )


def _authority_is_consumed(graph: _LockedMetaGraph) -> bool:
    return (
        graph.authorization.state == "CONSUMED"
        and graph.bounded.status == "CONSUMED"
        and graph.lease.status == "CONSUMED"
        and graph.consumption.state == "CONSUMED"
        and graph.budget.status == "CONSUMED"
        and graph.budget.reserved_count == 0
        and graph.budget.consumed_count == 1
        and graph.agent.execution_allowed is True
        and graph.agent.external_delivery_allowed is True
        and graph.agent.executed_at is not None
    )


def consume_meta_api_acceptance(
    session: Session,
    *,
    outbox_message_id: str,
    dispatcher_id: str,
    provider_message_id: str,
    now: datetime,
    admission_grace_seconds: int = ADMISSION_GRACE_SECONDS,
) -> bool:
    """Atomically consume authority and create the normalized schedule root."""

    timestamp = _utc(now)
    if admission_grace_seconds <= 0:
        raise ValueError("admission grace must be positive")
    if not provider_message_id or len(provider_message_id) > 255:
        raise SafetyDenied("PRODUCTION_META_ACCEPTANCE_STATE_INVALID")
    acquire_meta_attempt_gate(session, outbox_message_id)
    acquire_meta_provider_gate(session, provider_message_id)
    if session.scalar(
        select(HumanExecutionAuthorizationRow.id).where(
            HumanExecutionAuthorizationRow.request_wamid == provider_message_id
        )
    ) is not None:
        raise SafetyDenied("PRODUCTION_META_PROVIDER_SCOPE_COLLISION")
    existing = session.scalar(
        select(MetaDeliveryReconciliationRow)
        .where(MetaDeliveryReconciliationRow.outbox_message_id == outbox_message_id)
        .with_for_update(key_share=True)
    )
    if existing is not None:
        if existing.provider_message_id != provider_message_id:
            raise SafetyDenied("PRODUCTION_META_ACCEPTANCE_REPLAY_MISMATCH")
        graph = _lock_meta_graph(session, existing)
        if graph.outbox.claimed_by != dispatcher_id or not _authority_is_consumed(graph):
            raise SafetyDenied("PRODUCTION_META_ACCEPTANCE_REPLAY_MISMATCH")
        _, recovered_inbox, _ = _correlate_provider_inbox_in_transaction(
            session,
            provider_message_id=provider_message_id,
            now=timestamp,
            allow_exact_acceptance_requeue=True,
        )
        if recovered_inbox and existing.state == "PENDING":
            _reconcile_locked_meta_callback_outcome(
                session,
                existing,
                now=timestamp,
                correlate_pending_inbox=False,
            )
        return False

    outbox_snapshot = session.get(OutboxMessageRow, outbox_message_id)
    interaction = session.get(
        InteractionRow, outbox_snapshot.interaction_id if outbox_snapshot else None
    )
    if (
        outbox_snapshot is None
        or interaction is None
        or outbox_snapshot.status != "PROCESSING"
        or outbox_snapshot.claimed_by != dispatcher_id
        or outbox_snapshot.attempt_count != 1
        or outbox_snapshot.destination != META_DESTINATION
        or outbox_snapshot.action_type != META_ACTION_TYPE
        or outbox_snapshot.payload.get("provider_message_id")
    ):
        raise SafetyDenied("PRODUCTION_META_ACCEPTANCE_STATE_INVALID")

    deadline_at = timestamp + timedelta(seconds=RECONCILIATION_WINDOW_SECONDS)
    closure_after = deadline_at + timedelta(seconds=admission_grace_seconds)
    reconciliation = MetaDeliveryReconciliationRow(
        id=_stable_id("metarecon", outbox_message_id),
        tenant_id=interaction.tenant_id,
        outbox_message_id=outbox_message_id,
        provider_message_id=provider_message_id,
        api_accepted_at=timestamp,
        deadline_at=deadline_at,
        closure_after=closure_after,
        next_reconcile_at=closure_after,
        state="PENDING",
        terminal_reason=None,
        terminal_at=None,
        evidence_count=0,
        last_reconciled_at=None,
        created_at=timestamp,
        updated_at=timestamp,
        operational_state="ACTIVE",
        failure_count=0,
        last_failure_at=None,
        last_failure_code=None,
        quarantined_at=None,
        quarantine_reason=None,
    )
    session.add(reconciliation)
    session.flush()
    _, early_inbox_correlated, _ = _correlate_provider_inbox_in_transaction(
        session,
        provider_message_id=provider_message_id,
        now=timestamp,
        allow_exact_acceptance_requeue=True,
    )
    graph = _lock_meta_graph(session, reconciliation, accepting=True)
    if graph.outbox.claimed_by != dispatcher_id or not _authority_is_reserved(graph):
        raise SafetyDenied("PRODUCTION_META_ACCEPTANCE_AUTHORITY_INVALID")
    finalize_consumed_in_transaction(
        session,
        tenant_id=graph.interaction.tenant_id,
        lease_id=graph.lease.id,
        consumption_id=graph.consumption.id,
        logical_execution_id=graph.lease.logical_execution_id or "",
        logical_effect_id=graph.consumption.logical_effect_id,
        now=timestamp,
    )
    graph.authorization.state = "CONSUMED"
    graph.authorization.updated_at = timestamp
    graph.bounded.status = "CONSUMED"
    graph.bounded.updated_at = timestamp
    graph.agent.execution_allowed = True
    graph.agent.external_delivery_allowed = True
    graph.agent.executed_at = timestamp
    graph.outbox.payload = {
        **graph.outbox.payload,
        "provider_message_id": provider_message_id,
    }
    graph.outbox.status = "AWAITING_DELIVERY"
    audit(
        session,
        graph.outbox.interaction_id,
        "production_meta_api_accepted",
        {
            "outbox_id": graph.outbox.id,
            "provider_message_id_hash": provider_message_id_fingerprint(
                provider_message_id
            ),
        },
        correlation_id=graph.outbox.correlation_id,
        causation_id=graph.outbox.causation_id,
        tenant_id=graph.interaction.tenant_id,
        created_at=timestamp,
    )
    session.flush()
    if early_inbox_correlated:
        _reconcile_locked_meta_callback_outcome(
            session,
            reconciliation,
            now=timestamp,
            correlate_pending_inbox=False,
        )
    return True


def _evidence_for_reconciliation(
    session: Session,
    reconciliation_id: str,
) -> list[MetaCallbackEvidence]:
    rows = session.scalars(
        select(MetaCallbackEvidenceRow)
        .where(MetaCallbackEvidenceRow.reconciliation_id == reconciliation_id)
        .order_by(MetaCallbackEvidenceRow.received_at, MetaCallbackEvidenceRow.id)
    ).all()
    return [
        MetaCallbackEvidence(
            status=row.provider_status,
            provider_timestamp_raw=row.provider_timestamp_raw,
            received_at=row.received_at,
            errors_present=row.errors_present,
        )
        for row in rows
    ]


def _ensure_conflict_marker(
    session: Session,
    reconciliation: MetaDeliveryReconciliationRow,
    *,
    now: datetime,
) -> bool:
    rows = session.scalars(
        select(MetaCallbackEvidenceRow).where(
            MetaCallbackEvidenceRow.reconciliation_id == reconciliation.id,
            MetaCallbackEvidenceRow.valid.is_(True),
            MetaCallbackEvidenceRow.admissible.is_(True),
        )
    ).all()
    has_delivery = any(row.provider_status in _DELIVERY_STATUSES for row in rows)
    has_failure = any(row.provider_status == "failed" for row in rows)
    if not (has_delivery and has_failure):
        return False
    marker_type = "DELIVERY_WITH_FAILURE_CONFLICT"
    marker_id = _stable_id("metamarker", f"{reconciliation.id}:{marker_type}")
    if session.get(MetaCallbackAuditMarkerRow, marker_id) is not None:
        return False
    outbox = session.get(OutboxMessageRow, reconciliation.outbox_message_id)
    session.add(
        MetaCallbackAuditMarkerRow(
            id=marker_id,
            reconciliation_id=reconciliation.id,
            marker_type=marker_type,
            created_at=_utc(now),
        )
    )
    audit(
        session,
        outbox.interaction_id if outbox else None,
        "production_meta_delivery_failure_conflict",
        {
            "outbox_id": reconciliation.outbox_message_id,
            "delivery_evidence_present": True,
            "failure_evidence_present": True,
        },
        correlation_id=outbox.correlation_id if outbox else None,
        causation_id=outbox.causation_id if outbox else None,
        origin="meta_callback_reconciler_v2",
        tenant_id=reconciliation.tenant_id,
        created_at=_utc(now),
    )
    session.flush()
    return True


def _terminal_result(
    reconciliation: MetaDeliveryReconciliationRow,
    *,
    audit_state_changed: bool = False,
) -> MetaReconciliationResult:
    return MetaReconciliationResult(
        reconciliation_id=reconciliation.id,
        state_changed=audit_state_changed,
        domain_state_changed=False,
        terminalized=False,
        decision=reconciliation.state,
        reason_code=reconciliation.terminal_reason,
        evidence_class="ALREADY_TERMINAL",
        deadline_at=_utc(reconciliation.deadline_at),
        closure_after=_utc(reconciliation.closure_after),
    )


def _reconcile_locked_meta_callback_outcome(
    session: Session,
    reconciliation: MetaDeliveryReconciliationRow,
    *,
    now: datetime,
    correlate_pending_inbox: bool = True,
) -> MetaReconciliationResult:
    reconciliation = session.execute(
        select(MetaDeliveryReconciliationRow)
        .where(MetaDeliveryReconciliationRow.id == reconciliation.id)
        .with_for_update(key_share=True)
    ).scalar_one()
    if reconciliation.operational_state != "ACTIVE":
        raise SafetyDenied("PRODUCTION_META_RECONCILIATION_QUARANTINED")
    if correlate_pending_inbox:
        _correlate_pending_inbox_for_locked_reconciliation(
            session, reconciliation, now=now
        )
    if reconciliation.state in {"PASSED", "FAILED"}:
        marker_created = _ensure_conflict_marker(
            session, reconciliation, now=now
        )
        return _terminal_result(
            reconciliation, audit_state_changed=marker_created
        )
    inbox_backlog_id = session.scalar(
        select(MetaCallbackInboxRow.id)
        .where(
            MetaCallbackInboxRow.provider_message_id
            == reconciliation.provider_message_id,
            (MetaCallbackInboxRow.state == "PENDING")
            | (
                (MetaCallbackInboxRow.state == "QUARANTINED")
                & (
                    MetaCallbackInboxRow.last_error_code
                    == "PRODUCTION_META_RECONCILIATION_NOT_FOUND"
                )
            ),
        )
        .order_by(MetaCallbackInboxRow.received_at, MetaCallbackInboxRow.id)
        .limit(1)
    )
    if inbox_backlog_id is not None:
        schedule_changed = _utc(reconciliation.next_reconcile_at) != _utc(now)
        if schedule_changed:
            reconciliation.next_reconcile_at = _utc(now)
            reconciliation.updated_at = _utc(now)
            session.flush()
        return MetaReconciliationResult(
            reconciliation_id=reconciliation.id,
            state_changed=schedule_changed,
            domain_state_changed=False,
            terminalized=False,
            decision="PENDING",
            reason_code=None,
            evidence_class="INBOX_BACKLOG",
            deadline_at=_utc(reconciliation.deadline_at),
            closure_after=_utc(reconciliation.closure_after),
        )
    quarantined_inbox_id = session.scalar(
        select(MetaCallbackInboxRow.id)
        .where(
            MetaCallbackInboxRow.provider_message_id
            == reconciliation.provider_message_id,
            MetaCallbackInboxRow.state == "QUARANTINED",
            MetaCallbackInboxRow.valid.is_(True),
            MetaCallbackInboxRow.last_error_code
            != "PRODUCTION_META_OUTBOX_SCOPE_INVALID",
        )
        .order_by(MetaCallbackInboxRow.id)
        .limit(1)
    )
    if quarantined_inbox_id is not None:
        raise SafetyDenied("PRODUCTION_META_CALLBACK_INBOX_QUARANTINED")

    marker_created = _ensure_conflict_marker(session, reconciliation, now=now)
    graph = _lock_meta_graph(session, reconciliation)
    if (
        graph.outbox.status != "AWAITING_DELIVERY"
        or graph.run.status != "RUNNING"
        or not _authority_is_consumed(graph)
    ):
        raise SafetyDenied("PRODUCTION_META_RECONCILIATION_GRAPH_STATE_INVALID")
    failure_state_changed = (
        reconciliation.failure_count != 0
        or reconciliation.last_failure_at is not None
        or reconciliation.last_failure_code is not None
    )
    if failure_state_changed:
        reconciliation.failure_count = 0
        reconciliation.last_failure_at = None
        reconciliation.last_failure_code = None
    evidences = _evidence_for_reconciliation(session, reconciliation.id)
    evidence_count = len(evidences)
    decision = reduce_meta_callback_evidence(
        evidences,
        accepted_at=reconciliation.api_accepted_at,
        now=now,
        admission_grace_seconds=int(
            (_utc(reconciliation.closure_after) - _utc(reconciliation.deadline_at))
            .total_seconds()
        ),
    )

    if decision.decision == "PENDING":
        schedule_changed = _utc(reconciliation.next_reconcile_at) != _utc(
            reconciliation.closure_after
        )
        if schedule_changed:
            reconciliation.next_reconcile_at = reconciliation.closure_after
            reconciliation.updated_at = _utc(now)
            session.flush()
        return MetaReconciliationResult(
            reconciliation_id=reconciliation.id,
            state_changed=schedule_changed or marker_created or failure_state_changed,
            domain_state_changed=False,
            terminalized=False,
            decision="PENDING",
            reason_code=None,
            evidence_class=decision.evidence_class,
            deadline_at=decision.deadline_at,
            closure_after=decision.closure_after,
        )

    timestamp = _utc(now)
    reconciliation.state = decision.decision
    reconciliation.terminal_reason = decision.reason_code
    reconciliation.terminal_at = timestamp
    reconciliation.evidence_count = evidence_count
    reconciliation.last_reconciled_at = timestamp
    reconciliation.updated_at = timestamp
    graph.outbox.completed_at = timestamp
    graph.agent.blocked_reason = None
    if decision.decision == "PASSED":
        graph.outbox.status = "DONE"
        graph.outbox.last_error = None
        graph.agent.status = "SENT"
        transition_scenario_run(graph.run, ScenarioRunStatus.VERIFYING, now=timestamp)
        transition_scenario_run(
            graph.run,
            ScenarioRunStatus.PASSED,
            reason=META_DELIVERY_REASON,
            now=timestamp,
        )
        event_type = "production_conversation_reply_delivered"
    else:
        assert decision.reason_code is not None
        graph.outbox.status = "FAILED"
        graph.outbox.last_error = decision.reason_code
        graph.agent.status = "FAILED"
        graph.agent.blocked_reason = decision.reason_code
        transition_scenario_run(
            graph.run,
            ScenarioRunStatus.FAILED,
            reason=decision.reason_code,
            now=timestamp,
        )
        event_type = "production_conversation_reply_failed"
    graph.run.cleanup_state = "NOT_REQUIRED"
    audit(
        session,
        graph.interaction.id,
        event_type,
        {
            "execution_intent_id": graph.agent.execution_intent_id,
            "agent_execution_intent_id": graph.agent.id,
            "scenario_run_id": graph.run.id,
            "outbox_id": graph.outbox.id,
            "provider_message_id_hash": provider_message_id_fingerprint(
                reconciliation.provider_message_id
            ),
            "reason_code": decision.reason_code,
            "evidence_class": decision.evidence_class,
            "effect_count": 1,
            "evidence_count": evidence_count,
        },
        correlation_id=graph.run.root_correlation_id,
        causation_id=graph.outbox.causation_id,
        origin="meta_callback_reconciler_v2",
        tenant_id=graph.interaction.tenant_id,
    )
    session.flush()
    return MetaReconciliationResult(
        reconciliation_id=reconciliation.id,
        state_changed=True,
        domain_state_changed=True,
        terminalized=True,
        decision=decision.decision,
        reason_code=decision.reason_code,
        evidence_class=decision.evidence_class,
        deadline_at=decision.deadline_at,
        closure_after=decision.closure_after,
    )


def reconcile_meta_callback_outcome(
    session_factory: SessionFactory,
    *,
    reconciliation_id: str,
    now: datetime | None = None,
    transaction_timeout_seconds: int = 15,
) -> MetaReconciliationResult:
    """Reconcile exactly one graph in exactly one transaction."""

    if transaction_timeout_seconds <= 0:
        raise ValueError("Meta reconciliation transaction timeout must be positive")
    with session_factory() as session, session.begin():
        _apply_local_timeouts(
            session,
            transaction_limit_seconds=transaction_timeout_seconds,
            reconciliation=True,
        )
        reconciliation = session.execute(
            select(MetaDeliveryReconciliationRow)
            .where(MetaDeliveryReconciliationRow.id == reconciliation_id)
            .with_for_update(key_share=True)
        ).scalar_one()
        return _reconcile_locked_meta_callback_outcome(
            session, reconciliation, now=_utc(now or datetime.now(UTC))
        )


def reconcile_meta_callback_outcome_in_transaction(
    session: Session,
    *,
    reconciliation_id: str,
    now: datetime | None = None,
) -> MetaReconciliationResult:
    """Compatibility seam that still uses the single normalized reducer."""

    reconciliation = session.execute(
        select(MetaDeliveryReconciliationRow)
        .where(MetaDeliveryReconciliationRow.id == reconciliation_id)
        .with_for_update(key_share=True)
    ).scalar_one()
    return _reconcile_locked_meta_callback_outcome(
        session, reconciliation, now=_utc(now or datetime.now(UTC))
    )


def _record_row_failure(
    session_factory: SessionFactory,
    *,
    reconciliation_id: str,
    worker_id: str,
    error_type: str,
    now: datetime,
) -> None:
    try:
        with session_factory() as session, session.begin():
            reconciliation = session.execute(
                select(MetaDeliveryReconciliationRow)
                .where(MetaDeliveryReconciliationRow.id == reconciliation_id)
                .with_for_update(key_share=True)
            ).scalar_one_or_none()
            if (
                reconciliation is None
                or reconciliation.state != "PENDING"
                or reconciliation.operational_state != "ACTIVE"
            ):
                return
            failure_count = reconciliation.failure_count + 1
            reconciliation.failure_count = failure_count
            reconciliation.last_failure_at = _utc(now)
            reconciliation.last_failure_code = error_type[:120]
            if failure_count >= MAX_RECONCILIATION_FAILURES:
                reconciliation.operational_state = "QUARANTINED"
                reconciliation.quarantined_at = _utc(now)
                reconciliation.quarantine_reason = "MAX_RECONCILIATION_FAILURES"
            else:
                delay = min(
                    FAILURE_BACKOFF_MAX_SECONDS,
                    FAILURE_BACKOFF_BASE_SECONDS * (2 ** (failure_count - 1)),
                )
                reconciliation.next_reconcile_at = _utc(now) + timedelta(seconds=delay)
            reconciliation.updated_at = _utc(now)
            outbox = session.get(
                OutboxMessageRow,
                reconciliation.outbox_message_id,
            )
            audit(
                session,
                outbox.interaction_id if outbox else None,
                "production_meta_reconciliation_row_failed",
                {
                    "reconciliation_id": reconciliation_id,
                    "worker_id": worker_id,
                    "error_type": error_type,
                    "failure_count": failure_count,
                    "operational_state": reconciliation.operational_state,
                },
                correlation_id=outbox.correlation_id if outbox else None,
                causation_id=outbox.causation_id if outbox else None,
                origin="meta_callback_reconciler_v2",
                tenant_id=reconciliation.tenant_id,
                created_at=_utc(now),
            )
            session.flush()
    except Exception as exc:
        log_sanitized_exception(
            logger,
            "failed to persist Meta reconciliation row failure reconciliation_id=%s",
            reconciliation_id,
            exc=exc,
        )


def reconcile_due_meta_callback_outcomes(
    session_factory: SessionFactory,
    *,
    worker_id: str,
    now: datetime | None = None,
    limit: int = 100,
    transaction_timeout_seconds: int = 15,
) -> MetaSweepResult:
    """Select with SKIP LOCKED and commit or roll back one graph at a time."""

    if not 0 < limit <= 100:
        raise ValueError("Meta reconciliation batch limit must be between 1 and 100")
    if transaction_timeout_seconds <= 0:
        raise ValueError("Meta reconciliation transaction timeout must be positive")
    worker_id = _operational_label(worker_id, field="worker_id")
    timestamp = _utc(now or datetime.now(UTC))
    excluded: set[str] = set()
    selected = 0
    processed = 0
    terminalized = 0
    failed = 0

    while selected < limit:
        row_id: str | None = None
        try:
            with session_factory() as session, session.begin():
                _apply_local_timeouts(
                    session,
                    transaction_limit_seconds=transaction_timeout_seconds,
                    reconciliation=True,
                )
                query = select(MetaDeliveryReconciliationRow).where(
                    MetaDeliveryReconciliationRow.state == "PENDING",
                    MetaDeliveryReconciliationRow.operational_state == "ACTIVE",
                    MetaDeliveryReconciliationRow.next_reconcile_at <= timestamp,
                )
                if excluded:
                    query = query.where(
                        MetaDeliveryReconciliationRow.id.not_in(excluded)
                    )
                reconciliation = session.scalar(
                    query.order_by(
                        MetaDeliveryReconciliationRow.next_reconcile_at,
                        MetaDeliveryReconciliationRow.id,
                    )
                    .limit(1)
                    .with_for_update(skip_locked=True, key_share=True)
                )
                if reconciliation is None:
                    break
                row_id = reconciliation.id
                selected += 1
                result = _reconcile_locked_meta_callback_outcome(
                    session, reconciliation, now=timestamp
                )
                processed += 1
                terminalized += int(result.terminalized)
        except Exception as exc:
            if row_id is None:
                raise
            failed += 1
            excluded.add(row_id)
            _record_row_failure(
                session_factory,
                reconciliation_id=row_id,
                worker_id=worker_id,
                error_type=type(exc).__name__,
                now=timestamp,
            )
            log_sanitized_exception(
                logger,
                "Meta reconciliation row failed reconciliation_id=%s worker_id=%s",
                row_id,
                worker_id,
                exc=exc,
            )

    return MetaSweepResult(
        selected=selected,
        processed=processed,
        terminalized=terminalized,
        failed=failed,
    )


def recover_quarantined_meta_reconciliation(
    session: Session,
    *,
    reconciliation_id: str,
    operator_id: str,
    reason: str,
    now: datetime | None = None,
) -> bool:
    """Explicit, audited requeue seam; it never changes domain authority."""

    operator_id = _operational_label(operator_id, field="operator_id")
    reason = _requeue_reason(reason)
    timestamp = _utc(now or datetime.now(UTC))
    reconciliation = session.execute(
        select(MetaDeliveryReconciliationRow)
        .where(MetaDeliveryReconciliationRow.id == reconciliation_id)
        .with_for_update(key_share=True)
    ).scalar_one_or_none()
    if (
        reconciliation is None
        or reconciliation.state != "PENDING"
        or reconciliation.operational_state != "QUARANTINED"
    ):
        return False
    outbox = session.get(OutboxMessageRow, reconciliation.outbox_message_id)
    audit_id = new_id()
    session.add(
        AuditEventRow(
            id=audit_id,
            tenant_id=reconciliation.tenant_id,
            interaction_id=outbox.interaction_id if outbox else None,
            event_type="production_meta_reconciliation_requeued",
            correlation_id=outbox.correlation_id if outbox else None,
            causation_id=outbox.causation_id if outbox else None,
            previous_state="QUARANTINED",
            next_state="ACTIVE",
            policy_version_id=None,
            origin="meta_callback_reconciler_v2",
            payload={
                "reconciliation_id": reconciliation.id,
                "operator_fingerprint": hashlib.sha256(
                    operator_id.encode()
                ).hexdigest(),
                "reason_code": reason,
            },
            created_at=timestamp,
        )
    )
    session.flush()
    reconciliation.operational_state = "ACTIVE"
    reconciliation.failure_count = 0
    reconciliation.last_failure_at = None
    reconciliation.last_failure_code = None
    reconciliation.quarantined_at = None
    reconciliation.quarantine_reason = None
    reconciliation.next_reconcile_at = timestamp
    reconciliation.updated_at = timestamp
    reconciliation.last_requeue_audit_id = audit_id
    session.flush()
    return True


def recover_quarantined_meta_callback_inbox(
    session: Session,
    *,
    inbox_id: str,
    operator_id: str,
    reason: str,
    now: datetime | None = None,
) -> bool:
    """Explicit audited inbox requeue; no terminal graph or authority is changed."""

    operator_id = _operational_label(operator_id, field="operator_id")
    reason = _requeue_reason(reason)
    timestamp = _utc(now or datetime.now(UTC))
    provider_message_id = session.scalar(
        select(MetaCallbackInboxRow.provider_message_id).where(
            MetaCallbackInboxRow.id == inbox_id
        )
    )
    if provider_message_id is None:
        return False
    acquire_meta_provider_gate(session, provider_message_id)
    reconciliation = session.scalar(
        select(MetaDeliveryReconciliationRow)
        .where(
            MetaDeliveryReconciliationRow.provider_message_id
            == provider_message_id
        )
        .with_for_update(key_share=True)
    )
    if reconciliation is not None and reconciliation.state in {"PASSED", "FAILED"}:
        raise SafetyDenied("PRODUCTION_META_INBOX_REQUEUE_TERMINAL")
    inbox = session.scalar(
        select(MetaCallbackInboxRow)
        .where(MetaCallbackInboxRow.id == inbox_id)
        .with_for_update(key_share=True)
    )
    if inbox is None or inbox.state != "QUARANTINED":
        return False
    if (
        inbox.last_error_code
        and inbox.last_error_code.startswith("PRODUCTION_META_")
        and inbox.last_error_code != "PRODUCTION_META_RECONCILIATION_NOT_FOUND"
    ):
        raise SafetyDenied("PRODUCTION_META_INBOX_REQUEUE_SCOPE_REJECTED")

    outbox = (
        session.get(OutboxMessageRow, reconciliation.outbox_message_id)
        if reconciliation is not None
        else None
    )
    audit_id = new_id()
    session.add(
        AuditEventRow(
            id=audit_id,
            tenant_id=(
                reconciliation.tenant_id
                if reconciliation is not None
                else DEFAULT_TENANT_ID
            ),
            interaction_id=outbox.interaction_id if outbox is not None else None,
            event_type="production_meta_callback_inbox_requeued",
            correlation_id=outbox.correlation_id if outbox is not None else None,
            causation_id=outbox.causation_id if outbox is not None else None,
            previous_state="QUARANTINED",
            next_state="PENDING",
            policy_version_id=None,
            origin="meta_callback_reconciler_v2",
            payload={
                "inbox_id": inbox.id,
                "operator_fingerprint": hashlib.sha256(
                    operator_id.encode()
                ).hexdigest(),
                "reason_code": reason,
                "previous_attempt_count": inbox.correlation_attempt_count,
            },
            created_at=timestamp,
        )
    )
    session.flush()
    inbox.state = "PENDING"
    inbox.reconciliation_id = None
    inbox.correlation_attempt_count = 0
    inbox.next_correlation_at = timestamp
    inbox.last_error_code = None
    inbox.correlated_at = None
    inbox.quarantined_at = None
    inbox.claim_token = None
    inbox.claimed_by = None
    inbox.claimed_at = None
    inbox.last_requeue_audit_id = audit_id
    inbox.updated_at = timestamp
    session.flush()
    return True


__all__ = [
    "ADMISSION_GRACE_SECONDS",
    "MAX_ADMISSION_TRANSACTION_SECONDS",
    "META_ACTION_TYPE",
    "META_DESTINATION",
    "META_RECONCILIATION_GLOBAL_LOCK_ORDER",
    "MetaAdmissionResult",
    "MetaAdmissionUnavailable",
    "MetaCallbackEvidence",
    "MetaInboxSweepResult",
    "MetaReconciliationDecision",
    "MetaReconciliationResult",
    "MetaSweepResult",
    "RECONCILIATION_WINDOW_SECONDS",
    "admit_meta_callback_evidence",
    "consume_meta_api_acceptance",
    "parse_provider_timestamp",
    "persist_human_approval_delivery_evidence",
    "reconcile_due_meta_callback_outcomes",
    "reconcile_pending_meta_callback_inbox",
    "reconcile_meta_callback_outcome",
    "reconcile_meta_callback_outcome_in_transaction",
    "reduce_meta_callback_evidence",
    "recover_quarantined_meta_callback_inbox",
    "recover_quarantined_meta_reconciliation",
]
