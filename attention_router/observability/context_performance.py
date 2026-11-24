"""Bounded, content-free diagnostics for the private staged reader."""

from asyncio import CancelledError
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
import json
import logging
import sqlite3
from time import monotonic_ns, time_ns
from weakref import WeakSet

from sqlalchemy import event
from sqlalchemy.exc import DBAPIError, TimeoutError as PoolTimeout

from attention_router.config import settings
from attention_router.observability.tracing import extract_trace_context, start_span


class Stage(str, Enum):
    REQUEST = "context.request"
    INPUT = "context.input"
    EXECUTOR_WAIT = "context.executor_wait"
    WORKER = "context.worker"
    IDENTITY = "context.identity"
    CONSENT = "context.consent"
    ADMISSION = "context.admission"
    EMBEDDING = "context.embedding"
    INFERENCE_WAIT = "context.inference_wait"
    INFERENCE = "context.inference"
    CAPTURE = "context.capture"
    ANCHOR = "context.anchor"
    FENCE = "context.fence"
    VECTOR = "context.vector"
    SOURCE_FILTER = "context.source_filter"
    COMPILE = "context.compile"
    REBUILD = "context.segment_rebuild"
    RANK = "context.rank"
    GROUND = "context.ground"
    SERIALIZE = "context.serialize"
    REVALIDATE = "context.revalidate"
    SEND = "context.send"
    AFTER_DISCONNECT = "context.after_disconnect"


class Outcome(str, Enum):
    OK = "ok"
    GROUNDED = "grounded"
    ABSTENTION = "insufficient_evidence"
    INVALID = "invalid_request"
    AUTHORITY = "authority_refusal"
    REVOKED = "authority_changed"
    ADMISSION = "admission_budget"
    DISCONNECT = "client_disconnected"
    CANCELLED = "cancelled"
    APP_TIMEOUT = "application_timeout"
    DEADLINE = "compute_deadline"
    POOL = "pool_exhausted"
    LOCK = "database_lock_timeout"
    SQL_CANCEL = "sql_cancel_or_deadline"
    ANCHOR_LOCK = "anchor_lock"
    DEPENDENCY = "dependency_unavailable"
    INFERENCE = "inference_failure"
    INTERNAL = "internal_error"


@dataclass
class RequestSample:
    correlation: str
    trace: str | None = None
    disconnected: int | None = None
    result: Outcome = Outcome.OK
    answer: Outcome = Outcome.OK


@dataclass
class PhaseSample:
    sql_ns: int = 0
    sql_count: int = 0


request_sample = ContextVar("context_performance_request", default=None)
_phase_sample = ContextVar("context_performance_phase", default=None)
_observed_engines = WeakSet()
logger = logging.getLogger("attention_router.observability.private_context")


def emit(kind, stage, *, duration=None, outcome=None, count=None, sql=None):
    sample = request_sample.get()
    if sample is None:
        return
    fields = {"event": kind, "stage": stage.value, "timestamp_ns": time_ns(),
              "monotonic_ns": monotonic_ns(), "correlation": sample.correlation}
    if sample.trace:
        fields["trace"] = sample.trace
    if duration is not None:
        fields["duration_ns"] = duration
    if outcome is not None:
        fields["outcome"] = outcome.value
    if type(count) is int and 0 <= count <= 1_000_000:
        fields["count"] = count
    if sql is not None and sql.sql_count:
        fields.update(sql_ns=sql.sql_ns, sql_count=sql.sql_count)
    try:
        logger.info("g7_event %s", json.dumps(fields, separators=(",", ":")))
    except Exception:
        # A telemetry sink must not interfere with the disclosure boundary.
        pass


@contextmanager
def observe_query(correlation, traceparent):
    if not settings.context_performance_enabled:
        yield
        return
    sample = RequestSample(correlation)
    token = request_sample.set(sample)
    try:
        with phase(Stage.REQUEST, parent=extract_trace_context({"traceparent": traceparent})):
            yield
    finally:
        request_sample.reset(token)


def classify(error):
    from attention_router.application.client_session import (
        ClientSessionAuthorityRejected, ClientSessionUnauthenticated, ClientSessionUnavailable,
    )

    if isinstance(error, (ClientSessionAuthorityRejected, ClientSessionUnauthenticated, PermissionError)):
        return Outcome.AUTHORITY
    if isinstance(error, PoolTimeout):
        return Outcome.POOL
    if isinstance(error, TimeoutError):
        return Outcome.APP_TIMEOUT
    if isinstance(error, DBAPIError):
        return {"55P03": Outcome.LOCK, "57014": Outcome.SQL_CANCEL}.get(
            getattr(error.orig, "sqlstate", None), Outcome.DEPENDENCY,
        )
    if isinstance(error, ClientSessionUnavailable):
        return Outcome.DEPENDENCY
    cause = error
    for _ in range(4):
        if isinstance(cause, sqlite3.Error) and (getattr(cause, "sqlite_errorcode", 0) or 0) & 255 in (
            sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
        ):
            return Outcome.ANCHOR_LOCK
        cause = cause.__cause__
        if cause is None:
            break
    key = error.args[0] if isinstance(error, (ValueError, OSError)) and error.args else None
    if type(key) is str:
        return {
            "STAGED_CONVERSATION_COST_DENIED": Outcome.ADMISSION,
            "STAGED_CONVERSATION_SCOPE_REVOKED": Outcome.REVOKED,
            "STAGED_CONVERSATION_SUPPORT_CHANGED": Outcome.REVOKED,
            "STAGED_CONVERSATION_SUPPORT_EXPIRED": Outcome.REVOKED,
            "STAGED_ADMISSION_STALE": Outcome.REVOKED,
            "STAGED_DELIVERY_GENERATION_STALE": Outcome.REVOKED,
            "CONTEXT_CONSENT_REVISION_STALE": Outcome.REVOKED,
            "CONTEXT_CONSENT_NO_ELIGIBLE_SOURCE": Outcome.AUTHORITY,
            "CONTEXT_CONSENT_SOURCE_STALE": Outcome.REVOKED,
            "CONTEXT_EPOCH_AUTHORITY_UNAVAILABLE": Outcome.DEPENDENCY,
            "CONTEXT_INDEX_FENCE_INCOMPLETE": Outcome.DEPENDENCY,
            "CONVERSATION_EVIDENCE_NARROWER_SCOPE_REQUIRED": Outcome.ADMISSION,
            "STAGED_COMPUTE_EXPIRED": Outcome.DEADLINE,
            "STAGED_CONVERSATION_ENCODE_EXPIRED": Outcome.DEADLINE,
            "STAGED_SEND_CANCELLED": Outcome.CANCELLED,
            "STAGED_REQUEST_TOO_LARGE": Outcome.INVALID,
        }.get(key, Outcome.INTERNAL)
    return Outcome.INTERNAL


def result(outcome):
    sample = request_sample.get()
    if sample is not None:
        if sample.result not in (Outcome.OK, Outcome.GROUNDED, Outcome.ABSTENTION):
            return
        sample.result = outcome


@contextmanager
def phase(stage, *, parent=None):
    sample = request_sample.get()
    if sample is None:
        yield
        return
    start = monotonic_ns()
    stats = PhaseSample()
    token = _phase_sample.set(stats)
    outcome = Outcome.OK
    try:
        with start_span(stage.value, context=parent, attributes={
            "roc.correlation_id": sample.correlation,
        }) as span:
            if stage is Stage.REQUEST:
                context = span.get_span_context()
                sample.trace = f"{context.trace_id:032x}" if context.is_valid else None
            emit("begin", stage)
            try:
                yield
            except CancelledError:
                outcome = Outcome.CANCELLED
                raise
            except Exception as error:
                outcome = classify(error)
                if stage is Stage.INFERENCE and outcome is Outcome.INTERNAL:
                    outcome = Outcome.INFERENCE
                    result(outcome)
                raise
            finally:
                if stage is Stage.REQUEST:
                    outcome = sample.result
                span.set_attribute("g7.outcome", outcome.value)
                elapsed = monotonic_ns() - start
                span.set_attribute("g7.duration_ms", elapsed // 1_000_000)
                emit("end", stage, duration=elapsed, outcome=outcome, sql=stats)
    finally:
        _phase_sample.reset(token)


@contextmanager
def inference_lock(lock):
    if request_sample.get() is None:
        with lock:
            yield
        return
    with phase(Stage.INFERENCE_WAIT):
        lock.acquire()
    try:
        yield
    finally:
        lock.release()


def observe_database(engine):
    """SQL timings are observations of the driver boundary, including SQL locks."""
    if not settings.context_performance_enabled or engine in _observed_engines:
        return

    def before(_conn, _cursor, _statement, _parameters, execution, _many):
        stats = _phase_sample.get()
        if stats is not None:
            execution._andy_g7_sql = (stats, monotonic_ns())

    def account(execution):
        recorded = getattr(execution, "_andy_g7_sql", None)
        if recorded is not None:
            stats, started = recorded
            stats.sql_count += 1
            stats.sql_ns += monotonic_ns() - started

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", lambda *args: account(args[4]))
    event.listen(engine, "handle_error", lambda error: account(error.execution_context))
    _observed_engines.add(engine)
