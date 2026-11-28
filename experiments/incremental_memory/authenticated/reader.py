"""Authorized adapter for V0; no vector/graph/compiler, no memory writes."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import re
import uuid

from sqlalchemy import text

from attention_router.application.client_session import (
    ClientSessionService,
    ClientSessionAuthorityRejected,
)
from experiments.incremental_memory.memory import interpret, normalize, present


class StaleEvidence(ValueError):
    pass


@dataclass(frozen=True)
class Snapshot:
    identity: tuple
    scope: tuple
    rows: tuple


class MemoryAuthority:
    def __init__(self, factory, service: ClientSessionService):
        self.factory = factory
        self.service = service

    @contextmanager
    def guard(self, *, bearer, scope_ref):
        if not isinstance(scope_ref, str) or not re.fullmatch(
            r"ctx_[0-9a-f]{64}", scope_ref
        ):
            raise ClientSessionAuthorityRejected()
        with self.factory.begin() as session:
            session.execute(text("SET LOCAL lock_timeout='1s'"))
            session.execute(text("SET LOCAL statement_timeout='3s'"))
            # Every relevant writer acquires the exclusive counterpart via DB triggers.
            session.execute(text("SELECT pg_advisory_xact_lock_shared(721479,1)"))
            identity = self.service.authenticated_bootstrap(
                session, session_token=bearer
            )
            if not any(
                m.tenant_id == identity.active_tenant_id and m.role.value == "OWNER"
                for m in identity.memberships
            ):
                raise ClientSessionAuthorityRejected()
            scope = (
                session.execute(
                    text("""SELECT id,tenant_id,owner_id,purpose,active,expires_at,revision
                FROM memory_scopes WHERE id=:id"""),
                    {"id": scope_ref},
                )
                .mappings()
                .one_or_none()
            )
            if (
                scope is None
                or scope["tenant_id"] != identity.active_tenant_id
                or scope["owner_id"] != identity.human_identity_id
                or not scope["active"]
                or scope["purpose"] != "SYNTHETIC_MEMORY_QUERY"
                or scope["expires_at"] <= datetime.now(UTC)
            ):
                raise ClientSessionAuthorityRejected()
            yield session, identity, scope


def answer_authorized(session, identity, scope, question):
    """Retrieve only explicitly assigned information IDs, including current revisions."""
    query = interpret(question)
    rows = []
    if query:
        subject, attribute = query
        found = (
            session.execute(
                text("""SELECT i.id AS information_id,i.subject,i.attribute,
            r.number AS revision,r.value,e.id AS evidence_id,e.excerpt,e.origin,e.recorded_at
            FROM memory_scope_information a JOIN information i ON i.id=a.information_id
            JOIN revision r ON r.information_id=i.id AND r.number=i.current_revision
            JOIN evidence e ON e.id=r.evidence_id
            WHERE a.scope_id=:scope AND i.attribute=:attribute ORDER BY i.id"""),
                {"scope": scope["id"], "attribute": attribute},
            )
            .mappings()
            .all()
        )
        rows = [
            dict(row)
            for row in found
            if normalize(row["subject"]) == normalize(subject)
        ]
    return Snapshot(
        (
            identity.human_identity_id,
            identity.active_tenant_id,
            identity.device.device_id,
        ),
        tuple(scope.items()),
        tuple(rows),
    )


class AuthenticatedMemoryDelivery:
    """Implements StagedDelivery structurally; final callback is the disclosure boundary."""

    def __init__(self, authority):
        self.authority = authority

    def query_and_deliver(self, *, bearer, scope_ref, question, deliver):
        if (
            not callable(deliver)
            or not isinstance(question, str)
            or not 1 <= len(question.strip()) <= 1000
        ):
            raise ValueError("INVALID_QUERY")
        with self.authority.guard(bearer=bearer, scope_ref=scope_ref) as current:
            snapshot = answer_authorized(*current, question)
        body = json.dumps(
            present(snapshot.rows, str(uuid.uuid4())),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        if len(body) > 16384:
            raise ValueError("RESPONSE_TOO_LARGE")
        with self.authority.guard(bearer=bearer, scope_ref=scope_ref) as current:
            verified = answer_authorized(*current, question)
            if verified != snapshot:
                raise StaleEvidence("EVIDENCE_OR_SCOPE_CHANGED")
            # Recheck time-dependent session/scope expiry immediately before delivery.
            self.authority.service.authenticated_bootstrap(
                current[0], session_token=bearer
            )
            if current[2]["expires_at"] <= datetime.now(UTC):
                raise ClientSessionAuthorityRejected()
            deliver(body)
        return {"delivered_bytes": len(body)}
