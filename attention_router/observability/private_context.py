"""Content-free decision events, emitted only inside the private transport boundary."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
import logging
from time import monotonic
from uuid import uuid4

from sqlalchemy.exc import DBAPIError, TimeoutError as PoolTimeout


class Decision(str, Enum):
    OWNER = "OWNER_VALIDATED"
    SCOPE = "SCOPE_RESOLVED"
    CONSENT = "SOURCE_CONSENT_CONFIRMED"
    ADMITTED = "QUERY_ADMITTED"
    CANDIDATES = "CANDIDATE_SOURCES"
    DISCARDED = "SOURCES_WITHOUT_AUTHORIZATION"
    COMPILED = "COMPILED_EVIDENCE"
    REVOKED = "REVOCATION_OBSERVED"
    EPOCH = "EPOCH_INVALIDATED"
    ALLOWED = "DELIVERY_AUTHORIZED"
    SENT = "DELIVERED"
    BLOCKED = "DELIVERY_BLOCKED"
    REFUSED = "QUERY_REFUSED"
    TIMEOUT = "TIMEOUT"
    DATABASE = "DATABASE_FAILURE"
    INDEX = "INDEX_FAILURE"
    DISCONNECTED = "CLIENT_DISCONNECTED"


@dataclass
class QueryTrace:
    correlation: str
    started: float
    admitted: bool = False


_context = ContextVar("private_context_decision", default=None)
logger = logging.getLogger(__name__)


@contextmanager
def query_trace():
    value = str(uuid4())
    token = _context.set(QueryTrace(value, monotonic()))
    try:
        yield value
    finally:
        _context.reset(token)


def correlation_id():
    return _context.get().correlation


def record(event: Decision, count: int = 1):
    current = _context.get()
    if current is not None:
        if event is Decision.ADMITTED:
            current.admitted = True
        if event is Decision.BLOCKED and not current.admitted:
            record(Decision.REFUSED)
        logger.info("context_query correlation=%s state=%s count=%d elapsed_ms=%d",
                    current.correlation, event.value, count, round(1000 * (monotonic() - current.started)))


def record_failure(error):
    # Never stringify the exception, source, SQL or caller-provided fields.
    if isinstance(error, (PoolTimeout, TimeoutError)):
        record(Decision.TIMEOUT)
    elif isinstance(error, DBAPIError):
        timeout = getattr(error.orig, "sqlstate", None) in ("55P03", "57014")
        record(Decision.TIMEOUT if timeout else Decision.DATABASE)
    elif isinstance(error, ValueError) and error.args and type(error.args[0]) is str:
        code = error.args[0]
        if code.startswith("CONTEXT_EPOCH_") or code in (
            "STAGED_DELIVERY_GENERATION_STALE", "STAGED_ADMISSION_STALE",
        ):
            record(Decision.EPOCH)
        elif code in ("STAGED_COMPUTE_EXPIRED", "STAGED_CONVERSATION_ENCODE_EXPIRED"):
            record(Decision.TIMEOUT)
        elif code in ("CONTEXT_INDEX_FENCE_INCOMPLETE", "CONVERSATION_EVIDENCE_INDEX_CHANGED",
                      "RELEVANCE_POLICY_VERSION_MISMATCH", "STAGED_CONVERSATION_MODEL_CHANGED"):
            record(Decision.INDEX)
