"""Explicit, process-local authorization for Alembic's context fence migration.

This is a guard against accidental promotion, NOT a security boundary against
a database superuser. Only the private, reviewed release runner should enter
this context, after validating backup, current catalog, approval and locks.
"""

from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy.engine import Connection

_release_transaction: ContextVar[object | None] = ContextVar(
    "andy_context_schema_release_connection", default=None
)


@contextmanager
def approved_context_schema_release(connection: Connection):
    if not isinstance(connection, Connection) or not connection.in_transaction():
        raise RuntimeError("CONTEXT_SCHEMA_RELEASE_TRANSACTION_REQUIRED")
    token = _release_transaction.set(connection.get_transaction())
    try:
        yield
    finally:
        _release_transaction.reset(token)


def context_schema_release_authorized(connection: Connection) -> bool:
    return (
        isinstance(connection, Connection)
        and connection.in_transaction()
        and not connection.in_nested_transaction()
        and _release_transaction.get() is connection.get_transaction()
    )
