"""Test-only ClientSession adapter for a staged context read.

No public endpoint is registered here. The existing ClientContextReader and
all current execution, identity, and consent paths remain unchanged.
"""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from typing import Callable, Protocol

from sqlalchemy import text

from attention_router.application.client_context import ClientContextReader
from attention_router.application.context_canary_gate import canary_configured
from attention_router.application.client_session import ClientSessionAuthorityRejected
from attention_router.application.context_preactivation import require_context_database
from attention_router.application.context_epoch import ContextEpoch
from attention_router.application.context_pg_consent import PgSourceConsent
from attention_router.application.context_validity import ContextValidity
from attention_router.application.staged_read import (
    AuthorizedSnapshot, DetachedEvidence, StagedRead, StagedReadRejected,
)
from attention_router.config import settings
from attention_router.observability.private_context import Decision, record
from attention_router.infrastructure import client_session_repository as sessions
from attention_router.infrastructure.client_bootstrap_models import ClientDeviceRow
from attention_router.infrastructure.client_session_models import ClientSessionRow
from attention_router.infrastructure.models import TenantRow


class CaptureEvidence(Protocol):
    def __call__(
        self, context: ContextValidity, epoch: ContextEpoch, question: str, resource_id: str,
    ) -> DetachedEvidence: ...


class VerifyEvidence(Protocol):
    def __call__(
        self, context: ContextValidity, epoch: ContextEpoch, evidence: DetachedEvidence,
        resource_id: str,
    ) -> None: ...


def _prepare_transaction(reader, session, bearer, scope, *, pg_consent):
    require_context_database(session, reader_role=reader._role, require_pg_consent=pg_consent,
                             require_fence=pg_consent and settings.app_env == "production")
    session.execute(text("SET LOCAL lock_timeout = '4s'"))
    session.execute(text("SET LOCAL statement_timeout = '8s'"))
    reader._require_owner(session, bearer, scope)


def _authorized_context(reader, session, bearer, scope, permits):
    identity = reader._require_owner(session, bearer, scope)
    row = sessions.get_client_session_by_token(session, token=bearer)
    if row is None or row.device_id != identity.device.device_id:
        raise ClientSessionAuthorityRejected()
    reader._lock_authority_row(session, ClientDeviceRow, row.device_id)
    context = ContextValidity(
        session, tenant_id=scope.tenant_id,
        binding_id=scope.owner_binding_id,
        owner_human_identity_id=scope.owner_id,
        source_account=scope.source_account,
        start=scope.start, now=reader._clock(), source_allowed=permits,
    )
    reader._lock_authority_row(session, TenantRow, scope.tenant_id)
    reader._lock_authority_row(session, ClientSessionRow, row.id)
    reader._require_owner(session, bearer, scope)
    return context


class StagedClientContextAuthority:
    """Reuses the current authenticated ClientSession boundary.

    capture_fn builds detached evidence while authorized and locked.
    verify_fn MUST independently recheck every source dependency and version
    against current database/consent authority. Omitting it is forbidden.
    """

    def __init__(
        self,
        original: ClientContextReader,
        *,
        capture_fn: CaptureEvidence,
        verify_fn: VerifyEvidence,
        pg_consent: PgSourceConsent | None = None,
        owner_scope_registry=None,
    ):
        if not canary_configured():
            raise ValueError("STAGED_CONTEXT_PILOT_ONLY")
        if settings.app_env != "test" and (
            not isinstance(pg_consent, PgSourceConsent)
            or owner_scope_registry is None or original._scopes
        ):
            raise ValueError("STAGED_CONTEXT_PRODUCTION_AUTHORITY_REQUIRED")
        if not callable(capture_fn) or not callable(verify_fn):
            raise ValueError("STAGED_CONTEXT_CALLBACKS_REQUIRED")
        if owner_scope_registry is not None and pg_consent is None:
            raise ValueError("STAGED_OWNER_SCOPE_REQUIRES_PG_CONSENT")
        self._owner_scope_registry = owner_scope_registry
        self._pg_consent = pg_consent
        self._reader = original
        self._capture_fn = capture_fn
        self._verify_fn = verify_fn

    @contextmanager
    def _transaction(self, bearer, scope, expected_revision=None):
        reader = self._reader
        if self._pg_consent is None:
            with reader._consent.current(expected_revision) as (revision, permits), reader._sessions.begin() as session:
                _prepare_transaction(reader, session, bearer, scope, pg_consent=False)
                yield revision, _authorized_context(reader, session, bearer, scope, permits), permits
        else:
            with reader._sessions.begin() as session:
                _prepare_transaction(reader, session, bearer, scope, pg_consent=True)
                revision, permits = self._pg_consent.current(session, scope, expected_revision)
                yield revision, _authorized_context(reader, session, bearer, scope, permits), permits

    def _scope(self, scope_ref, bearer):
        if self._owner_scope_registry is not None:
            # Resolve afresh from persisted scope and current ClientSession
            # authority at each phase. The PgConsent transaction must then
            # revalidate the exact persisted fields before disclosing.
            with self._reader._sessions.begin() as session:
                return self._owner_scope_registry.resolve(
                    session, bearer=bearer, scope_ref=scope_ref,
                )
        scope = self._reader._scopes.get(scope_ref)
        if scope is None:
            raise ClientSessionAuthorityRejected()
        return scope

    def authorize_preparation(self, *, bearer: str, scope_ref: str):
        """Authenticate the caller before running a bounded embedding request."""
        scope = self._scope(scope_ref, bearer)
        with self._transaction(bearer, scope) as (_, context, permits):
            record(Decision.SCOPE)
            record(Decision.OWNER)
            if self._pg_consent is not None and self._owner_scope_registry is not None:
                self._pg_consent.require_current_source(
                    context.session, scope, scope_ref, permits, self._reader._clock(),
                )
                record(Decision.CONSENT)
            self._reader._epoch.token(context)
            return scope

    @contextmanager
    def capture(self, *, bearer: str, scope_ref: str, question: str):
        reader = self._reader
        scope = self._scope(scope_ref, bearer)
        with self._transaction(bearer, scope) as (revision, context, permits):
            session = context.session
            token = reader._epoch.token(context)
            evidence = self._capture_fn(context, reader._epoch, question, scope.resource_id)
            if not isinstance(evidence, DetachedEvidence):
                raise StagedReadRejected("STAGED_EVIDENCE_NOT_DETACHED")
            if self._pg_consent is not None:
                self._pg_consent.verify_sources(context, scope, evidence, permits, clock=reader._clock)
            if token != reader._epoch.token(context):
                raise StagedReadRejected("STAGED_ADMISSION_STALE")
            reader._require_owner(session, bearer, scope)
            yield AuthorizedSnapshot(
                scope_ref=scope_ref,
                question_digest=sha256(question.encode()).hexdigest(),
                consent_revision=revision,
                epoch_token=token,
                evidence=evidence,
            )

    @contextmanager
    def delivery_gate(self, *, bearer: str, scope_ref: str, question: str, snapshot):
        reader = self._reader
        scope = self._scope(scope_ref, bearer)
        if snapshot.scope_ref != scope_ref or snapshot.question_digest != sha256(question.encode()).hexdigest():
            raise StagedReadRejected("STAGED_DELIVERY_MISMATCH")
        # The consent revision check refuses any withdrawal since admission.
        with self._transaction(bearer, scope, snapshot.consent_revision) as (_, context, permits):
            session = context.session
            if snapshot.epoch_token != reader._epoch.token(context):
                raise StagedReadRejected("STAGED_DELIVERY_GENERATION_STALE")
            # REQUIRED: current source, receipt, membership, retention, revisions
            # and exact dependency checks. Must fail closed if support changed.
            check_expiry = self._verify_fn(context, reader._epoch, snapshot.evidence, scope.resource_id)
            if self._pg_consent is not None:
                self._pg_consent.verify_sources(context, scope, snapshot.evidence, permits, clock=reader._clock)
            reader._require_owner(session, bearer, scope)
            if snapshot.epoch_token != reader._epoch.token(context):
                raise StagedReadRejected("STAGED_DELIVERY_GENERATION_STALE")
            if check_expiry is not None:
                check_expiry()
            yield


class StagedClientContextReader:
    """Non-default, no-route experimental facade for StagedRead."""

    def __init__(
        self,
        original: ClientContextReader,
        *, capture_fn: CaptureEvidence, verify_fn: VerifyEvidence,
        compute_fn: Callable[[DetachedEvidence, str], bytes],
        pg_consent: PgSourceConsent | None = None, owner_scope_registry=None,
    ):
        authority = StagedClientContextAuthority(
            original, capture_fn=capture_fn, verify_fn=verify_fn,
            pg_consent=pg_consent, owner_scope_registry=owner_scope_registry,
        )
        self._pipeline = StagedRead(authority, compute_fn, max_body_bytes=16_384)

    def query_and_deliver(self, *, bearer, scope_ref, question, deliver):
        return self._pipeline.query_and_deliver(
            bearer=bearer, scope_ref=scope_ref, question=question,
            deliver=deliver,
        )
