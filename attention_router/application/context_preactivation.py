"""Internal synthetic preactivation entry point. No runtime registration."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from attention_router.application.context_consent import LabConsent
from attention_router.application.context_canary_gate import canary_configured
from attention_router.application.context_index_fence import FENCE_SCHEMA
from attention_router.application.context_epoch import AnchoredGeneration, ContextEpoch
from attention_router.application.context_validity import ContextValidity


CONTEXT_SCHEMA = "0070_context_integrity"
CONSENT_SCHEMA = "0071_context_source_consent"


def require_context_database(session: Session, *, reader_role: str, require_pg_consent=False) -> None:
    """Compare the authenticated database principal and installed revision."""
    if not canary_configured() or session.get_bind().dialect.name != "postgresql":
        raise ValueError("CONTEXT_PREACTIVATION_UNAVAILABLE")
    role = session.execute(text(
        "SELECT current_user, session_user, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication "
        "FROM pg_roles WHERE rolname = current_user"
    )).one()
    if not reader_role or role[0] != reader_role or role[1] != reader_role or any(role[2:]):
        raise ValueError("CONTEXT_DATABASE_ROLE_MISMATCH")
    revisions = session.scalars(text("SELECT version_num FROM alembic_version")).all()
    supported = (CONSENT_SCHEMA, FENCE_SCHEMA)
    if not require_pg_consent:
        supported += (CONTEXT_SCHEMA,)
    if len(revisions) != 1 or revisions[0] not in supported:
        raise ValueError("CONTEXT_DATABASE_SCHEMA_MISMATCH")


@dataclass(frozen=True)
class PreactivationToken:
    epoch: AnchoredGeneration
    consent_revision: str


class ContextPreactivation:
    def __init__(self, session_factory, *, role: str, epoch: ContextEpoch, consent: LabConsent, clock=None):
        self._sessions = session_factory
        self._role = role
        self._epoch = epoch
        self._consent = consent
        self._clock = clock or (lambda: datetime.now(UTC))

    def _context(self, session, permits, scope):
        require_context_database(session, reader_role=self._role)
        if "now" in scope:
            raise ValueError("CONTEXT_PREACTIVATION_CALLER_CLOCK_DENIED")
        return ContextValidity(session, source_allowed=permits, now=self._clock(), **scope)

    def observe(self, message_ids, **scope):
        with self._consent.current() as (_, permits), self._sessions.begin() as session:
            context = self._context(session, permits, scope)
            return self._epoch.advance(context, context.observe, tuple(message_ids))

    def compile(self, query: str, **scope):
        with self._consent.current() as (revision, permits), self._sessions.begin() as session:
            context = self._context(session, permits, scope)
            token, packet = self._epoch.compile(context, query)
            return PreactivationToken(token, revision), packet

    def consume(self, token: PreactivationToken, packet, **scope):
        with self._consent.current(token.consent_revision) as (_, permits), self._sessions.begin() as session:
            context = self._context(session, permits, scope)
            return self._epoch.consume(context, token.epoch, packet)
