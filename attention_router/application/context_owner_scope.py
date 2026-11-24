"""Resolve persisted context scope from an authenticated ClientSession OWNER.

No endpoint and no grant is created here. The returned scope is bound to an
existing consent-scope row, the active tenant, and the canonical owner binding.
"""

from __future__ import annotations

from datetime import UTC, datetime
from re import fullmatch

from sqlalchemy import or_, select

from attention_router.application.client_context import ClientContextScope
from attention_router.application.context_canary_gate import canary_configured, canary_scope
from attention_router.application.client_session import ClientSessionAuthorityRejected
from attention_router.application.context_pg_consent import PURPOSE
from attention_router.application.personal_context_bootstrap import resolve_represented_owner_actor_key
from attention_router.infrastructure.context_consent_models import ContextConsentScopeRow
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import ActorBindingRow, ResourceRow, TenantRow


def _deny():
    # Avoid revealing whether a particular owner/scope/source exists.
    raise ClientSessionAuthorityRejected()


class AuthenticatedOwnerScopeRegistry:
    """An existing scope ID is a locator, never proof of authority by itself."""

    def __init__(self, *, client_sessions, clock=None, auth_clock=None):
        if not callable(getattr(client_sessions, "authenticated_bootstrap", None)):
            raise ValueError("CONTEXT_SCOPE_AUTHORITY_REQUIRED")
        self._bootstrap = client_sessions.authenticated_bootstrap
        self._clock = clock or (lambda: datetime.now(UTC))
        self._auth_clock = auth_clock or self._clock

    def resolve(self, session, *, bearer: str, scope_ref: str) -> ClientContextScope:
        if not canary_configured():
            _deny()
        if not isinstance(scope_ref, str) or not fullmatch(r"ctx_[0-9a-f]{64}", scope_ref):
            _deny()
        if not session.in_transaction():
            raise ValueError("CONTEXT_SCOPE_TRANSACTION_REQUIRED")
        now = self._clock()
        identity = self._bootstrap(session, session_token=bearer, now=self._auth_clock())
        tenant_id = identity.active_tenant_id
        owner_id = identity.human_identity_id
        if not tenant_id or not owner_id or not any(
            m.tenant_id == tenant_id and m.role.value == "OWNER"
            for m in identity.memberships
        ):
            _deny()

        row = session.scalar(
            select(ContextConsentScopeRow).where(
                ContextConsentScopeRow.id == scope_ref[4:],
                ContextConsentScopeRow.tenant_id == tenant_id,
                ContextConsentScopeRow.owner_id == owner_id,
                ContextConsentScopeRow.purpose == PURPOSE,
            ).with_for_update(read=True).execution_options(populate_existing=True)
        )
        if row is None:
            _deny()
        if not canary_scope(
            tenant_id=row.tenant_id, binding_id=row.binding_id, owner_id=row.owner_id,
            source_account=row.source_account,
        ):
            _deny()
        start = row.scope_start
        if start is None or start.tzinfo is None or not start < now:
            _deny()
        if not row.source_account or row.source_account == "default":
            _deny()

        # Recompute the server-side deterministic scope identifier. A forged
        # row with a caller-selected ID must never become a valid capability.
        fields = {
            "tenant_id": row.tenant_id,
            "owner_id": row.owner_id,
            "binding_id": row.binding_id,
            "source_account": row.source_account,
            "resource_id": row.resource_id,
            "purpose": row.purpose,
            "scope_start": start.astimezone(UTC).isoformat(),
        }
        if stable_hash(fields) != row.id:
            _deny()

        tenant = session.get(TenantRow, tenant_id, populate_existing=True)
        resource = session.get(ResourceRow, row.resource_id, populate_existing=True)
        if (
            tenant is None or tenant.status != "ACTIVE"
            or resource is None or resource.tenant_id != tenant_id
            or resource.status != "ACTIVE"
        ):
            _deny()

        owners = session.scalars(
            select(ActorBindingRow).where(
                ActorBindingRow.tenant_id == tenant_id,
                ActorBindingRow.is_active.is_(True),
                or_(
                    ActorBindingRow.actor_category == "owner",
                    ActorBindingRow.binding_metadata["owner"].as_boolean().is_(True),
                ),
            ).order_by(ActorBindingRow.id).with_for_update(read=True)
            .execution_options(populate_existing=True)
        ).all()
        if len(owners) != 1 or owners[0].id != row.binding_id:
            _deny()
        actor_key = resolve_represented_owner_actor_key(
            session, tenant_id=tenant_id, owner_human_identity_id=owner_id,
        )
        if actor_key != owners[0].actor_key:
            _deny()

        return ClientContextScope(
            tenant_id=tenant_id,
            owner_id=owner_id,
            owner_binding_id=row.binding_id,
            source_account=row.source_account,
            start=start,
            resource_id=row.resource_id,
        )

    def enroll(self, session, *, bearer: str, message_id: str, capture_start: datetime) -> str:
        """Register a scope explicitly, without authorizing ANY source.

        The caller chooses only an existing message locator. Tenant, OWNER,
        source_account, resource, and binding are derived from authenticated
        rows. A fixed server-side capture_start is mandatory; this method
        never chooses an earlier history window on the client's behalf.
        """
        from sqlalchemy.dialects.postgresql import insert

        from attention_router.infrastructure.artifact_models import ArtifactReceiptRow, ArtifactRow
        from attention_router.infrastructure.models import ConversationMessageRow

        if (
            not canary_configured()
            or not session.in_transaction() or session.get_bind().dialect.name != "postgresql"
            or not isinstance(message_id, str) or len(message_id) > 64
            or capture_start is None or capture_start.utcoffset() is None
        ):
            _deny()
        now = self._clock()
        if not capture_start < now:
            _deny()
        identity = self._bootstrap(session, session_token=bearer, now=self._auth_clock())
        tenant_id, owner_id = identity.active_tenant_id, identity.human_identity_id
        if not tenant_id or not owner_id or not any(
            m.tenant_id == tenant_id and m.role.value == "OWNER"
            for m in identity.memberships
        ):
            _deny()

        message = session.scalar(select(ConversationMessageRow).where(
            ConversationMessageRow.id == message_id,
            ConversationMessageRow.tenant_id == tenant_id,
            ConversationMessageRow.source == "wwebjs",
            ConversationMessageRow.sent_at >= capture_start,
            ConversationMessageRow.searchable.is_(True),
        ).with_for_update(read=True).execution_options(populate_existing=True))
        if message is None or message.source_account == "default":
            _deny()
        if not canary_scope(
            tenant_id=tenant_id, owner_id=owner_id, source_account=message.source_account,
        ):
            _deny()
        meta = message.metadata_json or {}
        receipt_ref = meta.get("artifact_receipt_id")
        if not isinstance(receipt_ref, str):
            _deny()
        receipt = session.scalar(select(ArtifactReceiptRow).where(
            ArtifactReceiptRow.id == receipt_ref,
            ArtifactReceiptRow.tenant_id == tenant_id,
            ArtifactReceiptRow.source_account == message.source_account,
            ArtifactReceiptRow.source_channel == "channel.whatsapp",
        ).with_for_update(read=True).execution_options(populate_existing=True))
        if receipt is None or receipt.artifact_id != meta.get("artifact_id"):
            _deny()
        artifact = session.scalar(select(ArtifactRow).where(
            ArtifactRow.id == receipt.artifact_id,
            ArtifactRow.tenant_id == tenant_id,
            ArtifactRow.status == "AVAILABLE",
        ).with_for_update(read=True).execution_options(populate_existing=True))
        if artifact is None:
            _deny()
        resource = session.get(ResourceRow, artifact.resource_id, populate_existing=True)
        tenant = session.get(TenantRow, tenant_id, populate_existing=True)
        if (
            resource is None or resource.tenant_id != tenant_id or resource.status != "ACTIVE"
            or tenant is None or tenant.status != "ACTIVE"
        ):
            _deny()

        owners = session.scalars(select(ActorBindingRow).where(
            ActorBindingRow.tenant_id == tenant_id,
            ActorBindingRow.is_active.is_(True),
            or_(
                ActorBindingRow.actor_category == "owner",
                ActorBindingRow.binding_metadata["owner"].as_boolean().is_(True),
            ),
        ).order_by(ActorBindingRow.id).with_for_update(read=True)
        .execution_options(populate_existing=True)).all()
        if len(owners) != 1:
            _deny()
        actor_key = resolve_represented_owner_actor_key(
            session, tenant_id=tenant_id, owner_human_identity_id=owner_id,
        )
        if actor_key != owners[0].actor_key:
            _deny()
        if not canary_scope(
            tenant_id=tenant_id, owner_id=owner_id, binding_id=owners[0].id,
            source_account=message.source_account,
        ):
            _deny()

        fields = dict(
            tenant_id=tenant_id, owner_id=owner_id, binding_id=owners[0].id,
            source_account=message.source_account, resource_id=resource.id,
            purpose=PURPOSE, scope_start=capture_start,
        )
        key = stable_hash({**fields, "scope_start": capture_start.astimezone(UTC).isoformat()})
        # New scope starts at revision=0, with no source consent. Replays are
        # idempotent and never alter an existing grant/revoke decision.
        from uuid import uuid4

        session.execute(
            insert(ContextConsentScopeRow).values(
                id=key, incarnation=str(uuid4()), **fields,
            ).on_conflict_do_nothing(index_elements=[ContextConsentScopeRow.id])
        )
        created = session.scalar(select(ContextConsentScopeRow).where(
            ContextConsentScopeRow.id == key,
        ).with_for_update(read=True).execution_options(populate_existing=True))
        if created is None or any(getattr(created, k) != v for k, v in fields.items()):
            _deny()
        return "ctx_" + key
