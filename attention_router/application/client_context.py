"""Authenticated contextual read pilot. Requires the isolated consent and epoch authorities."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from sqlalchemy import select, text

from attention_router.application.client_session import ClientSessionAuthorityRejected
from attention_router.application.context_canary_gate import canary_configured
from attention_router.application.context_preactivation import require_context_database
from attention_router.application.context_validity import ContextValidity
from attention_router.config import settings
from attention_router.infrastructure import client_session_repository as sessions
from attention_router.infrastructure.client_bootstrap_models import ClientDeviceRow
from attention_router.infrastructure.client_session_models import ClientSessionRow
from attention_router.infrastructure.models import TenantRow


@dataclass(frozen=True)
class ClientContextScope:
    tenant_id: str
    owner_id: str
    owner_binding_id: str
    source_account: str
    start: datetime
    resource_id: str


class ClientContextReader:
    def __init__(
        self, session_factory, *, role, client_sessions, scope_registry: Mapping[str, ClientContextScope],
        consent, epoch, read: Callable, clock=None, auth_clock=None,
    ):
        if not canary_configured():
            raise ValueError("CLIENT_CONTEXT_PILOT_ONLY")
        if settings.app_env != "test" and (scope_registry or consent is not None):
            raise ValueError("CONTEXT_PRODUCTION_REQUIRES_DYNAMIC_SCOPE")
        self._sessions = session_factory
        self._role = role
        self._bootstrap = client_sessions.authenticated_bootstrap
        self._scopes = MappingProxyType(dict(scope_registry))
        self._consent = consent
        self._epoch = epoch
        self._read = read
        self._clock = clock or (lambda: datetime.now(UTC))
        self._auth_clock = auth_clock or (lambda: datetime.now(UTC))

    def _require_owner(self, session, bearer, scope):
        identity = self._bootstrap(session, session_token=bearer, now=self._auth_clock())
        if (
            scope is None or identity.active_tenant_id != scope.tenant_id
            or identity.human_identity_id != scope.owner_id
            or not any(m.tenant_id == scope.tenant_id and m.role.value == "OWNER"
                       for m in identity.memberships)
        ):
            raise ClientSessionAuthorityRejected()
        return identity

    @staticmethod
    def _lock_authority_row(session, model, key):
        authority = session.scalar(
            select(model).where(model.id == key).with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if authority is None:
            raise ClientSessionAuthorityRejected()

    def query(self, *, bearer, scope_ref, question):
        scope = self._scopes.get(scope_ref)
        with self._consent.current() as (_, permits), self._sessions.begin() as session:
            require_context_database(session, reader_role=self._role)
            session.execute(text("SET LOCAL lock_timeout = '4s'"))
            session.execute(text("SET LOCAL statement_timeout = '8s'"))
            identity = self._require_owner(session, bearer, scope)
            # Match session issuance: device before membership and tenant.
            row = sessions.get_client_session_by_token(session, token=bearer)
            if row is None or row.device_id != identity.device.device_id:
                raise ClientSessionAuthorityRejected()
            self._lock_authority_row(session, ClientDeviceRow, row.device_id)
            context = ContextValidity(
                session, tenant_id=scope.tenant_id, binding_id=scope.owner_binding_id,
                owner_human_identity_id=scope.owner_id, source_account=scope.source_account,
                start=scope.start, now=self._clock(), source_allowed=permits,
            )
            # Hold the existing binding/membership and session authority through
            # projection. A committed withdrawal is observed before disclosure.
            self._lock_authority_row(session, TenantRow, scope.tenant_id)
            self._lock_authority_row(session, ClientSessionRow, row.id)
            self._require_owner(session, bearer, scope)
            token = self._epoch.token(context)
            answer = self._read(context, self._epoch, question, scope.resource_id)
            if token != self._epoch.token(context):
                raise ValueError("CLIENT_CONTEXT_GENERATION_STALE")
            final_time = self._clock()
            if final_time != context.arguments["now"]:
                # Recompute with current expiry/retention and generation checks
                # when materializing the response, before releasing the locks.
                current = ContextValidity(
                    session, **{**context.arguments, "now": final_time}, source_allowed=permits,
                )
                if token != self._epoch.token(current):
                    raise ValueError("CLIENT_CONTEXT_GENERATION_STALE")
                if answer != self._read(current, self._epoch, question, scope.resource_id):
                    raise ValueError("CLIENT_CONTEXT_SUPPORT_CHANGED")
            self._require_owner(session, bearer, scope)
            return answer
