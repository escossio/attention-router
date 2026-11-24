"""Explicit OWNER-bound epoch enrollment for a private context canary.

This operator-only service never grants source access, never provisions on
read, and never creates credentials. A bootstrap approval must happen outside
the data-plane HTTP handlers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from attention_router.application.client_context_staged import (
    _authorized_context, _prepare_transaction,
)
from attention_router.application.context_epoch import ContextEpoch
from attention_router.application.context_owner_scope import AuthenticatedOwnerScopeRegistry
from attention_router.application.context_pg_consent import PgSourceConsent
from attention_router.infrastructure.context_epoch_durable import DurableEpochAuthority


@dataclass(frozen=True, slots=True)
class EpochEnrollmentProof:
    scope_ref: str
    domain: str
    checkpoint: str


class OwnerEpochProvisioner:
    def __init__(
        self, *, reader, owner_scope_registry: AuthenticatedOwnerScopeRegistry,
        environment: str, authority_id: str, key: bytes, path: Path,
    ):
        if (
            not isinstance(owner_scope_registry, AuthenticatedOwnerScopeRegistry)
            or not environment or not authority_id or not isinstance(key, bytes)
            or len(key) < 32 or not isinstance(path, Path)
        ):
            raise ValueError("CONTEXT_EPOCH_ENROLLMENT_CONFIGURATION_INVALID")
        self._reader = reader
        self._registry = owner_scope_registry
        self._environment = environment
        self._authority_id = authority_id
        self._key = key
        self._path = path

    def preview(self, *, bearer: str, scope_ref: str) -> EpochEnrollmentProof:
        """Derive a checkpoint from a CURRENT authenticated owner transaction."""
        reader = self._reader
        with reader._sessions.begin() as session:
            from attention_router.application.context_preactivation import require_context_database

            require_context_database(session, reader_role=reader._role, require_pg_consent=True)
            scope = self._registry.resolve(session, bearer=bearer, scope_ref=scope_ref)
            _prepare_transaction(reader, session, bearer, scope, pg_consent=True)
            _, permits = PgSourceConsent().current(session, scope)
            context = _authorized_context(reader, session, bearer, scope, permits)
            domain = ContextEpoch.domain_for(self._environment, context)
            checkpoint = ContextEpoch.checkpoint_for(context)
            reader._require_owner(session, bearer, scope)
            return EpochEnrollmentProof(scope_ref, domain, checkpoint)

    def provision_explicit(
        self, *, bearer: str, scope_ref: str, operator_approved: bool,
    ) -> DurableEpochAuthority:
        """Privileged one-shot action, never wired to an HTTP request handler.

        DB and independent CAS store cannot commit atomically. If evidence
        advances between snapshot and external provision, later reads fail
        closed on checkpoint mismatch. The anchor is never auto-repaired.
        """
        if operator_approved is not True:
            raise ValueError("CONTEXT_EPOCH_OPERATOR_APPROVAL_REQUIRED")
        proof = self.preview(bearer=bearer, scope_ref=scope_ref)
        anchor = DurableEpochAuthority.provision(
            self._path,
            authority_id=self._authority_id,
            key=self._key,
            domain=proof.domain,
            checkpoint=proof.checkpoint,
        )
        # Check that the PG checkpoint still matches the separate authority.
        current = self.preview(bearer=bearer, scope_ref=scope_ref)
        if current != proof:
            raise ValueError("CONTEXT_EPOCH_ENROLLMENT_RACE")
        return anchor
