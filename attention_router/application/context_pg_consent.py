"""Source consent in the staged reader's PostgreSQL transaction."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from attention_router.application.client_session import ClientSessionAuthorityRejected
from attention_router.application.context_canary_gate import canary_scope
from attention_router.application.context_consent import RETENTION_LAYERS, SourceConsent
from attention_router.application.platform.artifacts import context_receipt_refusal
from attention_router.infrastructure.artifact_models import ArtifactReceiptRow, ArtifactRow
from attention_router.infrastructure.context_consent_models import ContextConsentScopeRow, ContextSourceConsentRow
from attention_router.observability.private_context import Decision, record
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import ConversationMessageRow

PURPOSE = "prospective_context"
_BATCH_SIZE = 256


def _scope_fields(scope):
    return dict(
        tenant_id=scope.tenant_id, owner_id=scope.owner_id, binding_id=scope.owner_binding_id,
        source_account=scope.source_account, resource_id=scope.resource_id, purpose=PURPOSE,
        scope_start=scope.start,
    )


def _lock_scope(session, scope, *, write=False):
    if (
        scope is None or not canary_scope(
            tenant_id=scope.tenant_id, binding_id=scope.owner_binding_id,
            owner_id=scope.owner_id, source_account=scope.source_account,
        ) or not session.in_transaction() or session.in_nested_transaction()
        or session.get_bind().dialect.name != "postgresql"
    ):
        raise ValueError("CONTEXT_PG_CONSENT_UNAVAILABLE")
    connection = session.connection()
    if (
        connection.get_isolation_level() != "READ COMMITTED"
        or getattr(connection.connection.driver_connection, "autocommit", True)
    ):
        raise ValueError("CONTEXT_PG_CONSENT_UNAVAILABLE")
    if scope.start.utcoffset() is None:
        raise ValueError("CONTEXT_CONSENT_SCOPE_UNAVAILABLE")
    fields = _scope_fields(scope)
    key = stable_hash({**fields, "scope_start": scope.start.astimezone(UTC).isoformat()})
    if write:
        new_scope = insert(ContextConsentScopeRow).values(id=key, incarnation=str(uuid4()), **fields)
        session.execute(new_scope.on_conflict_do_nothing(index_elements=[ContextConsentScopeRow.id]))
    row = session.scalar(select(ContextConsentScopeRow).where(
        ContextConsentScopeRow.id == key,
    ).with_for_update(read=not write).execution_options(populate_existing=True))
    if row is None or any(getattr(row, name) != value for name, value in fields.items()):
        raise ValueError("CONTEXT_CONSENT_SCOPE_UNAVAILABLE")
    return row


def _receipt(session, scope, receipt_id):
    return session.scalar(select(ArtifactReceiptRow).where(
        ArtifactReceiptRow.id == receipt_id, ArtifactReceiptRow.tenant_id == scope.tenant_id,
        ArtifactReceiptRow.source_account == scope.source_account,
        ArtifactReceiptRow.source_channel == "channel.whatsapp",
    ).with_for_update(read=True).execution_options(populate_existing=True))


def _chunks(ids):
    """Bound each statement without reducing the existing evidence limit."""
    keys = sorted(set(ids))
    for start in range(0, len(keys), _BATCH_SIZE):
        yield keys[start:start + _BATCH_SIZE]


def _receipts(session, scope, receipt_ids):
    """Fresh scoped receipt locks, in deterministic order for this transaction."""
    result = {}
    for keys in _chunks(receipt_ids):
        rows = session.scalars(select(ArtifactReceiptRow).where(
            ArtifactReceiptRow.id.in_(keys),
            ArtifactReceiptRow.tenant_id == scope.tenant_id,
            ArtifactReceiptRow.source_account == scope.source_account,
            ArtifactReceiptRow.source_channel == "channel.whatsapp",
        ).order_by(ArtifactReceiptRow.id).with_for_update(read=True)
          .execution_options(populate_existing=True)).all()
        result.update((row.id, row) for row in rows)
    return result


def _grant(row, scope, purpose):
    return SourceConsent(
        row.decision_ref, scope.tenant_id, scope.owner_id, scope.source_account,
        row.conversation_id, row.message_id, row.receipt_id, purpose,
        row.starts_at, row.expires_at, row.sensitivity,
        tuple((layer, datetime.fromisoformat(deadline)) for layer, deadline in row.retention.items()),
    )


class _BatchedPermit:
    """A scalar permit plus a fresh, transaction-bound set projection."""

    def __init__(self, scalar, filter_rows):
        self._scalar = scalar
        self.filter_rows = filter_rows

    def __call__(self, message, *, owner_id, now, required_layers=()):
        return self._scalar(message, owner_id=owner_id, now=now, required_layers=required_layers)


def _digest(message):
    return stable_hash((message.content_hash, message.text, message.normalized_text))


def _matches(row, grant, message, receipt, *, owner_id, now, required_layers=()):
    return (
        row.revoked_at is None and receipt is not None
        and set(required_layers) <= RETENTION_LAYERS
        and grant.permits(message, owner_id=owner_id, now=now, required_layers=required_layers)
        and message.searchable
        and (message.metadata_json or {}).get("context_evidence_withdrawn") is not True
        and row.message_revision == message.context_revision and row.content_digest == _digest(message)
        and row.receipt_id == receipt.id and row.receipt_revision == receipt.context_revision
        and receipt.artifact_id == (message.metadata_json or {}).get("artifact_id")
        and not context_receipt_refusal(receipt.metadata_json, now=now)
    )


class PgSourceConsent:
    """Bind a scoped permit predicate to the caller's held transaction."""

    def current(self, session, scope, expected_revision=None, *, batch=False):
        authority = _lock_scope(session, scope)
        revision = f"{authority.id}:{authority.incarnation}:{authority.revision}"
        if expected_revision is not None and expected_revision != revision:
            raise ValueError("CONTEXT_CONSENT_REVISION_STALE")
        transaction = session.get_transaction()

        def require_transaction():
            if session.get_transaction() is not transaction or not transaction.is_active:
                raise ValueError("CONTEXT_CONSENT_TRANSACTION_ENDED")

        def permits(message, *, owner_id, now, required_layers=()):
            require_transaction()
            required_layers = tuple(required_layers)
            row = session.get(ContextSourceConsentRow, (authority.id, message.id))
            if row is None:
                return False
            if row.revoked_at is not None:
                record(Decision.REVOKED)
                return False
            grant = _grant(row, scope, authority.purpose)
            return _matches(
                row, grant, message, _receipt(session, scope, row.receipt_id),
                owner_id=owner_id, now=now, required_layers=required_layers,
            )

        if not batch:
            return revision, permits

        def filter_rows(messages, *, owner_id, now, required_layers=()):
            # Caller has already locked message rows in the same transaction.
            # No grants, receipts, permit decisions, or time samples are retained
            # between calls. Delivery must acquire a new scope/transaction.
            require_transaction()
            if session.in_nested_transaction():
                raise ValueError("CONTEXT_PG_CONSENT_UNAVAILABLE")
            messages = tuple(messages)
            if not messages:
                return ()
            if any(message.tenant_id != scope.tenant_id or
                   message.source_account != scope.source_account for message in messages):
                raise ValueError("CONTEXT_CONSENT_BATCH_SCOPE_MISMATCH")
            grants = {}
            for keys in _chunks(message.id for message in messages):
                rows = session.scalars(select(ContextSourceConsentRow).where(
                    ContextSourceConsentRow.scope_id == authority.id,
                    ContextSourceConsentRow.message_id.in_(keys),
                ).order_by(ContextSourceConsentRow.message_id)
                  .execution_options(populate_existing=True)).all()
                grants.update((row.message_id, row) for row in rows)
            receipts = _receipts(session, scope, (
                row.receipt_id for row in grants.values()
                if row.revoked_at is None and row.receipt_id
            ))
            allowed = []
            for message in messages:
                row = grants.get(message.id)
                if row is None:
                    continue
                if row.revoked_at is not None:
                    record(Decision.REVOKED)
                    continue
                stamp = now() if callable(now) else now
                layers = required_layers(message) if callable(required_layers) else required_layers
                if _matches(row, _grant(row, scope, authority.purpose), message,
                            receipts.get(row.receipt_id), owner_id=owner_id, now=stamp,
                            required_layers=tuple(layers)):
                    allowed.append(message)
            require_transaction()
            return tuple(allowed)

        return revision, _BatchedPermit(permits, filter_rows)

    @staticmethod
    def require_current_source(session, scope, scope_ref, permits, now):
        # An existing scope alone never pays for an encode or opens the index.
        # Bound preflight work. A scope requiring a larger scan fails closed.
        decisions = ContextSourceConsentRow
        candidates = session.scalars(select(ConversationMessageRow.id).join(
            decisions, decisions.message_id == ConversationMessageRow.id,
        ).where(
            decisions.scope_id == scope_ref[4:],
            decisions.revoked_at.is_(None), decisions.expires_at > now,
            ConversationMessageRow.tenant_id == scope.tenant_id,
            ConversationMessageRow.source_account == scope.source_account,
            ConversationMessageRow.sent_at >= scope.start,
        ).order_by(decisions.message_id).limit(256)).all()
        for message_id in candidates:
            message = session.get(ConversationMessageRow, message_id)
            if message is None:
                continue
            layers = ("embedding", "transcription") if message.message_type == "VOICE" else ("embedding",)
            if permits(message, owner_id=scope.owner_id, now=now, required_layers=layers):
                return
        raise ValueError("CONTEXT_CONSENT_NO_ELIGIBLE_SOURCE")

    def verify_sources(self, context, scope, evidence, permits, *, clock):
        if not evidence.sources:
            raise ValueError("CONTEXT_CONSENT_NO_ELIGIBLE_SOURCE")
        versions = {source.source_ref: source for source in evidence.sources}
        messages = context.lock_permitted_source_rows(versions)
        if len(messages) != len(versions):
            raise ValueError("CONTEXT_CONSENT_SOURCE_STALE")
        if isinstance(permits, _BatchedPermit):
            receipts = _receipts(context.session, scope, (v.receipt_ref for v in versions.values()))
            for message in messages:
                version = versions[message.id]
                receipt = receipts.get(version.receipt_ref)
                if (
                    message.context_revision != version.revision
                    or version.receipt_ref != (message.metadata_json or {}).get("artifact_receipt_id")
                    or receipt is None or receipt.context_revision != version.receipt_revision
                ):
                    raise ValueError("CONTEXT_CONSENT_SOURCE_STALE")
            # Independently reload the grant and its receipt; sample the current
            # clock per source rather than reusing the capture timestamp.
            if len(permits.filter_rows(messages, owner_id=scope.owner_id, now=clock)) != len(messages):
                raise ValueError("CONTEXT_CONSENT_SOURCE_STALE")
            return
        for message in messages:
            version = versions[message.id]
            receipt = _receipt(context.session, scope, version.receipt_ref)
            if (
                message.context_revision != version.revision
                or version.receipt_ref != (message.metadata_json or {}).get("artifact_receipt_id")
                or receipt is None or receipt.context_revision != version.receipt_revision
                or not permits(message, owner_id=scope.owner_id, now=clock())
            ):
                raise ValueError("CONTEXT_CONSENT_SOURCE_STALE")


class PgSourceConsentWriter:
    """Use a restricted, separately configured ClientContextReader for writes."""

    def __init__(self, authority_reader, *, owner_scope_registry=None):
        self._reader = authority_reader
        self._owner_scope_registry = owner_scope_registry

    @contextmanager
    def _write(self, bearer, scope_ref, message_id):
        from attention_router.application.client_context_staged import _authorized_context, _prepare_transaction
        from attention_router.application.context_preactivation import require_context_database

        reader = self._reader
        with reader._sessions.begin() as session:
            if self._owner_scope_registry is None:
                scope = reader._scopes.get(scope_ref)
                if scope is None:
                    raise ClientSessionAuthorityRejected()
            else:
                require_context_database(session, reader_role=reader._role, require_pg_consent=True)
                scope = self._owner_scope_registry.resolve(
                    session, bearer=bearer, scope_ref=scope_ref,
                )
            _prepare_transaction(reader, session, bearer, scope, pg_consent=True)
            authority = _lock_scope(session, scope, write=True)
            _authorized_context(reader, session, bearer, scope, None)
            current = session.get(ContextSourceConsentRow, (authority.id, message_id))
            yield session, scope, authority, current
            reader._require_owner(session, bearer, scope)

    @staticmethod
    def _message(session, scope, message_id):
        message = session.scalar(select(ConversationMessageRow).where(
            ConversationMessageRow.id == message_id,
            ConversationMessageRow.tenant_id == scope.tenant_id,
            ConversationMessageRow.source_account == scope.source_account,
            ConversationMessageRow.source == "wwebjs",
            ConversationMessageRow.sent_at >= scope.start,
        ).with_for_update(read=True).execution_options(populate_existing=True))
        if message is None:
            raise ValueError("CONTEXT_CONSENT_SOURCE_UNAVAILABLE")
        return message

    def grant(
        self, *, bearer, scope_ref, decision_ref, message_id, conversation_id, receipt_id,
        message_revision, receipt_revision, starts_at, expires_at, sensitivity, retention,
    ):
        with self._write(bearer, scope_ref, message_id) as (session, scope, authority, current):
            return self._record_grant(
                session, scope, authority, current, message_id=message_id,
                decision_ref=decision_ref, conversation_id=conversation_id, receipt_id=receipt_id,
                message_revision=message_revision, receipt_revision=receipt_revision,
                starts_at=starts_at, expires_at=expires_at, sensitivity=sensitivity,
                retention=retention, now=self._reader._clock(),
            )

    def grant_current(self, *, bearer, scope_ref, message_id, decision_ref, expires_at, retention):
        """Seal an explicit decision against the source versions held by this transaction."""
        with self._write(bearer, scope_ref, message_id) as (session, scope, authority, current):
            now = self._reader._clock()
            if (
                expires_at.utcoffset() is None or not now < expires_at <= now + timedelta(days=30)
                or set(retention) != RETENTION_LAYERS
                or any(deadline.utcoffset() is None or not now < deadline <= expires_at
                       for deadline in retention.values())
            ):
                raise ValueError("CONTEXT_CONSENT_DEADLINE_INVALID")
            message = self._message(session, scope, message_id)
            meta = message.metadata_json or {}
            receipt_id = meta.get("artifact_receipt_id")
            receipt = _receipt(session, scope, receipt_id) if isinstance(receipt_id, str) else None
            if (
                receipt is None
                or receipt.external_receipt_id != f"whatsapp:{message.source_message_id}:media"
                or not message.context_revision or not receipt.context_revision
                or meta.get("deleted") is True or meta.get("revoked") is True
                or meta.get("content_state") in ("DELETED", "REVOKED")
                or context_receipt_refusal(receipt.metadata_json, now=now)
            ):
                raise ValueError("CONTEXT_CONSENT_SOURCE_INELIGIBLE")
            receipt_deadline = (receipt.metadata_json or {}).get("context_expires_at")
            if receipt_deadline is not None and expires_at > datetime.fromisoformat(receipt_deadline):
                raise ValueError("CONTEXT_CONSENT_DEADLINE_INVALID")
            artifact = session.scalar(select(ArtifactRow).where(
                ArtifactRow.id == receipt.artifact_id, ArtifactRow.tenant_id == scope.tenant_id,
                ArtifactRow.resource_id == scope.resource_id, ArtifactRow.status == "AVAILABLE",
            ).with_for_update(read=True).execution_options(populate_existing=True))
            if artifact is None:
                raise ValueError("CONTEXT_CONSENT_SOURCE_INELIGIBLE")
            return self._record_grant(
                session, scope, authority, current, message=message, receipt=receipt,
                message_id=message_id, decision_ref=decision_ref, conversation_id=message.conversation_id,
                receipt_id=receipt.id,
                message_revision=message.context_revision, receipt_revision=receipt.context_revision,
                starts_at=message.sent_at, expires_at=expires_at, sensitivity="NORMAL",
                retention=retention.items(), now=now,
            )

    def _record_grant(
        self, session, scope, authority, current, *, message_id, decision_ref, conversation_id,
        receipt_id, message_revision, receipt_revision, starts_at, expires_at, sensitivity, retention, now,
        message=None, receipt=None,
    ):
        grant = SourceConsent(
            decision_ref, scope.tenant_id, scope.owner_id, scope.source_account,
            conversation_id, message_id, receipt_id, PURPOSE, starts_at, expires_at,
            sensitivity, tuple(retention),
        )
        fields = dict(
            decision_ref=decision_ref, conversation_id=conversation_id, receipt_id=receipt_id,
            message_revision=message_revision, receipt_revision=receipt_revision, sensitivity=sensitivity,
            starts_at=starts_at, expires_at=expires_at,
            retention={layer: deadline.astimezone(UTC).isoformat() for layer, deadline in grant.retention},
        )
        if current is not None:
            if current.revoked_at is not None:
                raise ValueError("CONTEXT_CONSENT_REVOKED")
            if any(getattr(current, name) != value for name, value in fields.items()):
                raise ValueError("CONTEXT_CONSENT_DECISION_CONFLICT")
            return False
        if message is None:
            message = self._message(session, scope, message_id)
            receipt = _receipt(session, scope, receipt_id)
        row = ContextSourceConsentRow(
            scope_id=authority.id, message_id=message_id, content_digest=_digest(message), **fields,
        )
        if not _matches(row, grant, message, receipt, owner_id=scope.owner_id, now=now):
            raise ValueError("CONTEXT_CONSENT_SOURCE_INELIGIBLE")
        session.add(row)
        authority.revision += 1
        return True

    def revoke_current(self, *, bearer, scope_ref, message_id):
        return self.revoke(bearer=bearer, scope_ref=scope_ref, message_id=message_id, _check_resource=True)

    def revoke(self, *, bearer, scope_ref, message_id, _check_resource=False):
        with self._write(bearer, scope_ref, message_id) as (session, scope, authority, current):
            if current is None:
                message = self._message(session, scope, message_id)
                if _check_resource:
                    artifact = session.scalar(select(ArtifactRow).where(
                        ArtifactRow.id == (message.metadata_json or {}).get("artifact_id"),
                        ArtifactRow.tenant_id == scope.tenant_id, ArtifactRow.resource_id == scope.resource_id,
                    ).with_for_update(read=True).execution_options(populate_existing=True))
                    if artifact is None:
                        raise ValueError("CONTEXT_CONSENT_SOURCE_UNAVAILABLE")
                current = ContextSourceConsentRow(scope_id=authority.id, message_id=message_id)
                session.add(current)
            if current.revoked_at is not None:
                return False
            current.revoked_at = self._reader._clock()
            authority.revision += 1
            return True
