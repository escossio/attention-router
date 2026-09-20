import logging
import socket
import time

from sqlalchemy import inspect, select

from attention_router.application.decision_pipeline import process_agent_decision
from attention_router.application.autonomy import evaluate_and_route
from attention_router.application.execution import enqueue_ready_intents, probe_transport_ready
from attention_router.application.services import process_due_timers, process_outbox
from attention_router.application.owner_reply_grace import (
    grace_allows_interaction,
    process_due_grace_windows,
)
from attention_router.application.memory import process_memory_ingestion_jobs
from attention_router.application.personal_context_runtime import (
    PersonalContextRuntimeCycleResult,
    run_personal_context_runtime_cycle,
)
from attention_router.application.personal_context_authority_runtime import (
    PersonalContextAuthorityRuntimeResult,
    run_personal_context_authority_cycle,
)
from attention_router.application.personal_context_materialization_runtime import (
    PersonalContextMaterializationRuntimeResult,
    run_personal_context_materialization_cycle,
)
from attention_router.application.voice_transcription import (
    is_voice_input_event,
    process_voice_transcriptions,
    voice_decision_readiness,
)
from attention_router.application.voice_tts import process_tts_derivations
from attention_router.application.voice_media import cleanup_expired_media
from attention_router.application.platform.capability_pack import process_due_scheduled_events
from attention_router.config import settings
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.models import InboundEventRow, QueueRow
from attention_router.infrastructure.repository import audit
from attention_router.platform.meta_callback_reconciliation import (
    reconcile_due_meta_callback_outcomes,
    reconcile_pending_meta_callback_inbox,
)
from attention_router.observability.tracing import extract_trace_context, identifier_hash, safe_set_attribute, set_outcome, start_span


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("attention-router-worker")


def process_agent_decisions(session, worker: str, limit: int = 10) -> int:
    rows = session.scalars(
        select(QueueRow)
        .where(QueueRow.kind == "decision", QueueRow.status.in_(["PENDING", "pending"]))
        .order_by(QueueRow.created_at)
        .limit(limit)
    ).all()
    processed = 0
    for queue in rows:
        if queue.payload.get("grace_window_id") and not grace_allows_interaction(
            session, queue.payload["interaction_id"]
        ):
            queue.status = "CANCELED"
            queue.processed_at = now_utc()
            audit(
                session,
                queue.payload["interaction_id"],
                "grace.decision_suppressed",
                {"queue_id": queue.id, "reason": "CANCELED_BY_HUMAN_REPLY"},
                origin="owner_reply_grace",
            )
            continue
        event = session.get(InboundEventRow, queue.payload.get("event_id"))
        if event is not None:
            voice_state, voice_reason = voice_decision_readiness(session, event)
            if voice_state == "WAITING":
                queue.status = "WAITING_TRANSCRIPTION"
                queue.processed_at = None
                audit(
                    session,
                    queue.payload["interaction_id"],
                    "voice.decision_waiting",
                    {"queue_id": queue.id, "reason": voice_reason},
                    origin="voice_transcription",
                )
                continue
            if voice_state == "FAILED":
                queue.status = "CANCELED"
                queue.processed_at = now_utc()
                audit(
                    session,
                    queue.payload["interaction_id"],
                    "voice.decision_canceled",
                    {"queue_id": queue.id, "reason": voice_reason},
                    origin="voice_transcription",
                )
                continue
        queue.status = "PROCESSING"
        session.flush()
        queue_id = queue.id
        try:
            carrier = ((queue.payload or {}).get("observability") or {}).get("trace_context")
            with start_span("worker.dispatch", context=extract_trace_context(carrier)) as worker_span:
                event_id = queue.payload.get("event_id")
                safe_set_attribute(worker_span, "attention.inbound_event_id", identifier_hash(event_id) if event_id else None)
                decision = process_agent_decision(session, queue.payload["event_id"])
                if decision is not None:
                    safe_set_attribute(worker_span, "attention.decision_id", decision.id)
                    session.expire_all()
                    if grace_allows_interaction(session, queue.payload["interaction_id"]):
                        evaluate_and_route(session, decision)
                    else:
                        audit(
                            session,
                            queue.payload["interaction_id"],
                            "grace.decision_suppressed",
                            {"queue_id": queue.id, "reason": "CANCELED_DURING_DECISION"},
                            origin="owner_reply_grace",
                        )
                elif event is not None and is_voice_input_event(event):
                    voice_state, voice_reason = voice_decision_readiness(session, event)
                    if not grace_allows_interaction(
                        session, queue.payload["interaction_id"]
                    ):
                        queue.status = "CANCELED"
                        queue.processed_at = now_utc()
                    elif voice_state == "FAILED":
                        queue.status = "CANCELED"
                        queue.processed_at = now_utc()
                    else:
                        queue.status = (
                            "WAITING_TRANSCRIPTION"
                            if voice_state == "WAITING"
                            else "PENDING"
                        )
                        queue.processed_at = None
                    audit(
                        session,
                        queue.payload["interaction_id"],
                        "voice.decision_deferred",
                        {"queue_id": queue.id, "reason": voice_reason},
                        origin="voice_transcription",
                    )
                    continue
            queue.status = (
                "DONE"
                if grace_allows_interaction(session, queue.payload["interaction_id"])
                else "CANCELED"
            )
            queue.processed_at = now_utc()
            processed += 1
        except Exception as exc:
            session.rollback()
            event_id = (queue.payload or {}).get("event_id")
            event = session.get(InboundEventRow, event_id) if event_id else None
            if event and event.interaction_id:
                reason = "LAB_INBOUND_MEMBERSHIP_CONFLICT" if "lab_conversation_inbounds" in str(exc) else type(exc).__name__.upper()
                audit(
                    session,
                    event.interaction_id,
                    "NO_RESPONSE",
                    {"no_response_stage": "LAB_SESSION_CLAIM", "no_response_reason_code": reason},
                    origin="agent_decision_worker",
                )
                session.flush()
            logger.exception("agent decision failed queue_id=%s error=%s", queue_id, exc)
            break
    return processed


def process_scheduled_events_if_available(session) -> int:
    """Keep the worker live when the optional scheduler schema is not deployed yet."""
    if not inspect(session.bind).has_table("reminders"):
        return 0
    return process_due_scheduled_events(session)


def process_personal_context_runtime_if_due(
    session,
    *,
    now_monotonic: float,
    last_run_monotonic: float | None,
) -> tuple[PersonalContextRuntimeCycleResult | None, float | None]:
    if not settings.personal_context_runtime_enabled:
        return None, last_run_monotonic
    if (
        last_run_monotonic is not None
        and now_monotonic - last_run_monotonic
        < settings.personal_context_runtime_interval_seconds
    ):
        return None, last_run_monotonic

    result = run_personal_context_runtime_cycle(
        session,
        delivery_enabled=(
            settings.personal_context_recommendation_delivery_enabled
        ),
        suggestion_delivery_enabled=(
            settings.personal_context_suggestion_delivery_enabled
        ),
        owner_limit=settings.personal_context_runtime_owner_limit,
    )
    return result, now_monotonic


def process_personal_context_authority_runtime_if_due(
    session,
    *,
    now_monotonic: float,
    last_run_monotonic: float | None,
) -> tuple[PersonalContextAuthorityRuntimeResult | None, float | None]:
    if not settings.personal_context_authority_runtime_enabled:
        return None, last_run_monotonic
    if (
        last_run_monotonic is not None
        and now_monotonic - last_run_monotonic
        < settings.personal_context_runtime_interval_seconds
    ):
        return None, last_run_monotonic

    result = run_personal_context_authority_cycle(
        session,
        owner_limit=settings.personal_context_runtime_owner_limit,
    )
    return result, now_monotonic


def process_personal_context_materialization_runtime_if_due(
    session,
    *,
    now_monotonic: float,
    last_run_monotonic: float | None,
) -> tuple[PersonalContextMaterializationRuntimeResult | None, float | None]:
    if not settings.personal_context_materialization_runtime_enabled:
        return None, last_run_monotonic
    if (
        last_run_monotonic is not None
        and now_monotonic - last_run_monotonic
        < settings.personal_context_runtime_interval_seconds
    ):
        return None, last_run_monotonic

    result = run_personal_context_materialization_cycle(
        session,
        intent_limit=settings.personal_context_materialization_intent_limit,
    )
    return result, now_monotonic


def run_forever() -> None:
    identity = f"{socket.gethostname()}:{new_id()}"
    last_personal_context_run_monotonic: float | None = None
    last_personal_context_authority_run_monotonic: float | None = None
    last_personal_context_materialization_run_monotonic: float | None = None
    logger.info("worker started worker_id=%s", identity)
    while True:
        transport_ready = probe_transport_ready()
        with SessionLocal() as session:
            grace_count = process_due_grace_windows(session, identity)
            session.commit()
            transcription_count = process_voice_transcriptions(session, identity)
            decision_count = process_agent_decisions(session, identity)
            session.commit()
            execution_count = enqueue_ready_intents(
                session,
                transport_ready=transport_ready,
            )
            session.commit()
            tts_count = process_tts_derivations(session, identity)
            outbox_count = process_outbox(session, identity)
            timer_count = process_due_timers(session, identity)
            scheduled_event_count = process_scheduled_events_if_available(session)
            media_cleanup_count = cleanup_expired_media(session)
            if settings.memory_ingestion_enabled:
                with start_span("memory.extract") as memory_span:
                    memory_count = process_memory_ingestion_jobs(session)
                    safe_set_attribute(memory_span, "attention.memory.candidate_count", memory_count)
                    set_outcome(memory_span, "PROCESSED" if memory_count else "NO_JOBS")
            else:
                memory_count = 0
            session.commit()

            personal_context_result = None
            personal_context_now = time.monotonic()
            try:
                (
                    personal_context_result,
                    last_personal_context_run_monotonic,
                ) = process_personal_context_runtime_if_due(
                    session,
                    now_monotonic=personal_context_now,
                    last_run_monotonic=(
                        last_personal_context_run_monotonic
                    ),
                )
                if personal_context_result is not None:
                    session.commit()
            except Exception as exc:
                session.rollback()
                logger.exception(
                    "personal context runtime cycle failed "
                    "worker_id=%s error=%s",
                    identity,
                    exc,
                )

            personal_context_authority_result = None
            personal_context_authority_now = time.monotonic()
            try:
                (
                    personal_context_authority_result,
                    last_personal_context_authority_run_monotonic,
                ) = process_personal_context_authority_runtime_if_due(
                    session,
                    now_monotonic=personal_context_authority_now,
                    last_run_monotonic=(
                        last_personal_context_authority_run_monotonic
                    ),
                )
                if personal_context_authority_result is not None:
                    session.commit()
            except Exception as exc:
                session.rollback()
                logger.exception(
                    "personal context authority runtime cycle failed "
                    "worker_id=%s error=%s",
                    identity,
                    exc,
                )

            personal_context_materialization_result = None
            personal_context_materialization_now = time.monotonic()
            try:
                (
                    personal_context_materialization_result,
                    last_personal_context_materialization_run_monotonic,
                ) = process_personal_context_materialization_runtime_if_due(
                    session,
                    now_monotonic=personal_context_materialization_now,
                    last_run_monotonic=(
                        last_personal_context_materialization_run_monotonic
                    ),
                )
                if personal_context_materialization_result is not None:
                    session.commit()
            except Exception as exc:
                session.rollback()
                logger.exception(
                    "personal context materialization runtime cycle failed "
                    "worker_id=%s error=%s",
                    identity,
                    exc,
                )

        meta_inbox = reconcile_pending_meta_callback_inbox(
            SessionLocal,
            worker_id=identity,
            limit=settings.meta_reconciliation_batch_size,
            transaction_timeout_seconds=(
                settings.meta_admission_transaction_timeout_seconds
            ),
        )
        meta_reconciliation = reconcile_due_meta_callback_outcomes(
            SessionLocal,
            worker_id=identity,
            limit=settings.meta_reconciliation_batch_size,
            transaction_timeout_seconds=(
                settings.meta_reconciliation_transaction_timeout_seconds
            ),
        )
        if (
            grace_count
            or transcription_count
            or decision_count
            or execution_count
            or tts_count
            or outbox_count
            or timer_count
            or scheduled_event_count
            or memory_count
            or media_cleanup_count
            or (
                personal_context_result is not None
                and (
                    personal_context_result.hypotheses_persisted
                    or personal_context_result.recommendations_persisted
                    or personal_context_result.recommendations_enqueued
                    or personal_context_result.expired_recommendations
                )
            )
            or (
                personal_context_authority_result is not None
                and (
                    personal_context_authority_result.assessments_completed
                    or personal_context_authority_result.assessments_failed
                )
            )
            or (
                personal_context_materialization_result is not None
                and (
                    personal_context_materialization_result.materialized
                    or personal_context_materialization_result.retired
                    or personal_context_materialization_result.expired
                    or personal_context_materialization_result.approval_required
                    or personal_context_materialization_result.authority_blocked
                    or personal_context_materialization_result.execution_blocked
                    or personal_context_materialization_result.malformed
                    or personal_context_materialization_result.failed
                )
            )
            or meta_reconciliation.selected
            or meta_inbox.selected
        ):
            logger.info(
                "processed worker_id=%s grace_count=%s decision_count=%s outbox_count=%s "
                "timer_count=%s scheduled_event_count=%s "
                "personal_context_hypotheses=%s "
                "personal_context_recommendations=%s "
                "personal_context_enqueued=%s "
                "personal_context_authority_assessments=%s "
                "personal_context_intents_prepared=%s "
                "personal_context_materialized=%s "
                "personal_context_materialization_blocked=%s "
                "meta_reconciliation_processed=%s "
                "meta_reconciliation_failed=%s meta_inbox_correlated=%s",
                identity,
                grace_count,
                decision_count,
                outbox_count,
                timer_count,
                scheduled_event_count,
                (
                    personal_context_result.hypotheses_persisted
                    if personal_context_result is not None
                    else 0
                ),
                (
                    personal_context_result.recommendations_persisted
                    if personal_context_result is not None
                    else 0
                ),
                (
                    personal_context_result.recommendations_enqueued
                    if personal_context_result is not None
                    else 0
                ),
                (
                    personal_context_authority_result.assessments_completed
                    if personal_context_authority_result is not None
                    else 0
                ),
                (
                    personal_context_authority_result.intents_prepared
                    if personal_context_authority_result is not None
                    else 0
                ),
                (
                    personal_context_materialization_result.materialized
                    if personal_context_materialization_result is not None
                    else 0
                ),
                (
                    (
                        personal_context_materialization_result.approval_required
                        + personal_context_materialization_result.authority_blocked
                        + personal_context_materialization_result.execution_blocked
                    )
                    if personal_context_materialization_result is not None
                    else 0
                ),
                meta_reconciliation.processed,
                meta_reconciliation.failed,
                meta_inbox.correlated,
            )
        time.sleep(settings.worker_poll_interval_seconds)


if __name__ == "__main__":
    run_forever()
