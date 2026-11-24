"""Observe lease boundaries without issuing SQL or changing the pool policy."""

from asyncio import CancelledError
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from time import monotonic_ns
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.pool import QueuePool

from attention_router.observability.context_performance import (
    Outcome, RequestSample, Stage, classify, emit, request_sample,
)


@dataclass
class Lease:
    identifier: str
    sample: RequestSample
    started: int
    acquired: int | None = None
    backend_pid: int | None = None
    sql_started: int | None = None
    sql_ns: int = 0
    sql_count: int = 0
    begins: int = 0
    commits: int = 0
    rollbacks: int = 0
    lock_timeout_count: int = 0

    def report(self, operation, *, duration=None, outcome=None, **data):
        emit(operation, Stage.IDENTITY, duration=duration, outcome=outcome, sample=self.sample,
             pool={"pool_lease": self.identifier, **data})


_key = "andy_context_performance_lease"
_acquiring = ContextVar("andy_context_performance_checkout", default=None)
_active: dict[str, Lease] = {}
_guard = Lock()


def active_leases():
    """Private, in-process input for the opt-in lab sampler. Omit PID from events."""
    with _guard:
        return tuple(_active.values())


def report_driver_wait(lease, started, wait_type):
    if wait_type == "Lock" and started is not None and lease.sql_started == started:
        lease.report("pool_pg_lock_sample")


def report_progress(lease):
    now = monotonic_ns()
    sql = lease.sql_started
    elapsed = now - lease.acquired
    driver = lease.sql_ns + (max(0, now - sql) if sql is not None else 0)
    lease.report("pool_progress", duration=elapsed, pool_sql_active=sql is not None,
                 pool_sql_count=lease.sql_count, pool_sql_ns=driver,
                 pool_outside_cursor_ns=max(0, elapsed - driver),
                 pool_begins=lease.begins, pool_commits=lease.commits, pool_rollbacks=lease.rollbacks,
                 pool_lock_timeouts=lease.lock_timeout_count)


def _lease(connection):
    if connection.closed or connection.invalidated:
        return None
    return connection.connection.record_info.get(_key)


def observe_pool(engine):
    pool = engine.pool
    if not isinstance(pool, QueuePool):
        return
    connect = pool.connect

    def acquire():
        sample = request_sample.get()
        if sample is None:
            return connect()
        lease = Lease(uuid4().hex, sample, monotonic_ns())
        token = _acquiring.set(lease)
        lease.report("pool_request", pool_limit=pool.size())
        try:
            return connect()
        except (Exception, CancelledError) as error:
            outcome = Outcome.CANCELLED if isinstance(error, CancelledError) else classify(error)
            lease.report("pool_acquire_failed", duration=monotonic_ns() - lease.started,
                         outcome=outcome)
            raise
        finally:
            _acquiring.reset(token)

    def checkout(dbapi, record, _proxy):
        lease = _acquiring.get()
        if lease is None:
            return
        lease.acquired = monotonic_ns()
        lease.backend_pid = getattr(getattr(dbapi, "pgconn", None), "backend_pid", None)
        record.record_info[_key] = lease
        with _guard:
            _active[lease.identifier] = lease
        lease.report("pool_checkout", duration=lease.acquired - lease.started,
                     pool_limit=pool.size(), pool_used=pool.checkedout())

    def checkin(_dbapi, record):
        lease = record.record_info.pop(_key, None)
        if lease is None:
            return
        released = monotonic_ns()
        with _guard:
            _active.pop(lease.identifier, None)
        held = released - lease.acquired
        disconnected = lease.sample.disconnected
        lease.report(
            "pool_checkin", duration=held, pool_limit=pool.size(),
            pool_used=max(0, pool.checkedout() - 1),
            pool_sql_count=lease.sql_count, pool_sql_ns=lease.sql_ns,
            pool_outside_cursor_ns=max(0, held - lease.sql_ns),
            pool_begins=lease.begins, pool_commits=lease.commits, pool_rollbacks=lease.rollbacks,
            pool_lock_timeouts=lease.lock_timeout_count,
            pool_after_disconnect_ns=max(0, released - max(disconnected, lease.acquired)) if disconnected else 0,
        )

    def before(connection, _cursor, _statement, _parameters, execution, _many):
        lease = _lease(connection)
        if lease is not None:
            lease.sql_started = monotonic_ns()
            execution._g7_pool_lease = lease

    def after(execution, error=None):
        lease = getattr(execution, "_g7_pool_lease", None)
        if lease is None or lease.sql_started is None:
            return
        lease.sql_ns += monotonic_ns() - lease.sql_started
        lease.sql_count += 1
        lease.sql_started = None
        if getattr(error, "sqlstate", None) == "55P03":
            lease.lock_timeout_count += 1
            lease.report("pool_pg_lock_timeout", outcome=Outcome.LOCK)

    def transaction(field):
        def count(connection):
            lease = _lease(connection)
            if lease is not None:
                setattr(lease, field, getattr(lease, field) + 1)
        return count

    event.listen(pool, "checkout", checkout)
    event.listen(pool, "checkin", checkin)
    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", lambda *args: after(args[4]))
    event.listen(engine, "handle_error", lambda error: after(error.execution_context, error.original_exception))
    for edge, field in (("begin", "begins"), ("commit", "commits"), ("rollback", "rollbacks")):
        event.listen(engine, edge, transaction(field))
    pool.connect = acquire
