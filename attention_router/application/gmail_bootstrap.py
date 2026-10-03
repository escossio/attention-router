"""Owner product and runtime source for Gmail Semantic Bootstrap V0."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.client_session import ClientSessionService
from attention_router.application.gmail_connection import (
    GMAIL_READONLY_SCOPE,
    gmail_authorization_scope,
)
from attention_router.application.gmail_product_runner import (
    GmailProductAuthorizationInvalid,
    GmailProductAuthorizationUnavailable,
    GmailProductProviderUnavailable,
    GmailProductRefreshFailed,
    GmailProductRunner,
    _bearer_token,
    _sanitized,
)
from attention_router.application.personal_context_bootstrap import (
    create_bootstrap_run,
    queue_bootstrap_run,
    resolve_represented_owner_actor_key,
)
from attention_router.application.personal_context_bootstrap_product import (
    PersonalContextBootstrapProductBudgetInvalid,
    PersonalContextBootstrapProductError,
    PersonalContextBootstrapProductTenantForbidden,
)
from attention_router.config import Settings
from attention_router.infrastructure.gmail_slot_lock import acquire_gmail_slot
from attention_router.infrastructure.models import IntegrationBindingRow
from attention_router.infrastructure.personal_context_bootstrap_models import (
    PersonalContextBootstrapRunRow,
)
from attention_router.infrastructure.provider_authorization_models import (
    ProviderAuthorizationRow,
)
from attention_router.integrations.gmail_api_reader import (
    StaticGmailAccessTokenProvider,
)
from attention_router.integrations.gmail_bootstrap import (
    GmailBootstrapApiReader,
    GmailBootstrapHistoryAdapter,
)


class GmailBootstrapProductDisabled(PersonalContextBootstrapProductError):
    code = "GMAIL_BOOTSTRAP_DISABLED"


class GmailBootstrapConnectionRequired(PersonalContextBootstrapProductError):
    code = "GMAIL_BOOTSTRAP_CONNECTION_REQUIRED"


class GmailBootstrapReadonlyRequired(PersonalContextBootstrapProductError):
    code = "GMAIL_BOOTSTRAP_READONLY_REQUIRED"


class GmailBootstrapBindingInvalid(PersonalContextBootstrapProductError):
    code = "GMAIL_BOOTSTRAP_BINDING_INVALID"


class GmailBootstrapSourceError(RuntimeError):
    code = "GMAIL_BOOTSTRAP_SOURCE_UNAVAILABLE"

    def __init__(self):
        super().__init__(self.code)


class GmailBootstrapSourceDisabled(GmailBootstrapSourceError):
    code = "GMAIL_BOOTSTRAP_SOURCE_DISABLED"


class GmailBootstrapSourceBusy(GmailBootstrapSourceError):
    code = "GMAIL_BOOTSTRAP_SOURCE_BUSY"


class GmailBootstrapSourceAuthorityInvalid(GmailBootstrapSourceError):
    code = "GMAIL_BOOTSTRAP_SOURCE_AUTHORITY_INVALID"


class GmailBootstrapProductService:
    """Create Gmail-backed BootstrapRuns from existing owner authority."""

    def __init__(
        self,
        *,
        settings: Settings,
        client_sessions: ClientSessionService,
    ):
        self.settings = settings
        self.client_sessions = client_sessions

    def _require_enabled(self) -> None:
        if (
            not self.settings.personal_context_bootstrap_enabled
            or not self.settings.gmail_bootstrap_enabled
        ):
            raise GmailBootstrapProductDisabled()

    def _authority(self, session: Session, session_token: str | None):
        self._require_enabled()
        authority = self.client_sessions.authenticated_bootstrap(
            session,
            session_token=session_token,
        )
        canary = self.settings.personal_context_bootstrap_canary_tenant_id
        if canary is not None and authority.active_tenant_id != canary:
            raise PersonalContextBootstrapProductTenantForbidden()
        return authority

    @staticmethod
    def _gmail_authorization(
        session: Session,
        *,
        tenant_id: str,
        human_identity_id: str,
    ) -> tuple[ProviderAuthorizationRow, IntegrationBindingRow]:
        row = session.scalar(
            select(ProviderAuthorizationRow).where(
                ProviderAuthorizationRow.tenant_id == tenant_id,
                ProviderAuthorizationRow.human_identity_id
                == human_identity_id,
                ProviderAuthorizationRow.provider == "GOOGLE",
                ProviderAuthorizationRow.product == "GMAIL",
                ProviderAuthorizationRow.status == "ACTIVE",
                ProviderAuthorizationRow.revoked_at.is_(None),
            )
        )
        if row is None:
            raise GmailBootstrapConnectionRequired()
        if gmail_authorization_scope(row.granted_scopes) != GMAIL_READONLY_SCOPE:
            raise GmailBootstrapReadonlyRequired()

        binding = session.get(
            IntegrationBindingRow,
            row.integration_binding_id,
        )
        expected_account = f"sha256:{row.provider_account_hash}"
        if (
            binding is None
            or not binding.active
            or binding.tenant_id != tenant_id
            or binding.name != "channel.email"
            or binding.kind != "CHANNEL"
            or binding.account_key != expected_account
        ):
            raise GmailBootstrapBindingInvalid()
        return row, binding

    def create_and_queue(
        self,
        session: Session,
        *,
        session_token: str | None,
        consent_ref: str,
        processing_budget: dict[str, int] | None = None,
    ) -> PersonalContextBootstrapRunRow:
        authority = self._authority(session, session_token)
        _authorization, binding = self._gmail_authorization(
            session,
            tenant_id=authority.active_tenant_id,
            human_identity_id=authority.human_identity_id,
        )

        limit = self.settings.gmail_bootstrap_snapshot_limit
        budget = {
            "page_size": min(25, limit),
            "max_messages_per_chat": limit,
            "max_total_messages": limit,
        }
        if processing_budget is not None:
            if not isinstance(processing_budget, dict):
                raise PersonalContextBootstrapProductBudgetInvalid()
            unknown = set(processing_budget) - set(budget)
            if unknown:
                raise PersonalContextBootstrapProductBudgetInvalid()
            budget.update(processing_budget)
        if (
            isinstance(budget["page_size"], bool)
            or not isinstance(budget["page_size"], int)
            or not 1 <= budget["page_size"] <= limit
            or isinstance(budget["max_messages_per_chat"], bool)
            or not isinstance(budget["max_messages_per_chat"], int)
            or not 1 <= budget["max_messages_per_chat"] <= limit
            or isinstance(budget["max_total_messages"], bool)
            or not isinstance(budget["max_total_messages"], int)
            or not 1 <= budget["max_total_messages"] <= limit
        ):
            raise PersonalContextBootstrapProductBudgetInvalid()

        owner_actor_key = resolve_represented_owner_actor_key(
            session,
            tenant_id=authority.active_tenant_id,
            owner_human_identity_id=authority.human_identity_id,
        )
        row, _created = create_bootstrap_run(
            session,
            tenant_id=authority.active_tenant_id,
            owner_human_identity_id=authority.human_identity_id,
            represented_owner_actor_key=owner_actor_key,
            source_kind="GMAIL_TEXT",
            source_account=binding.account_key or "",
            source_revision="gmail-readonly-snapshot-v0",
            source_selection={
                "chat_keys": [GmailBootstrapHistoryAdapter.MAILBOX_KEY]
            },
            consent_ref=consent_ref,
            processing_budget=budget,
        )
        if row.state == "CREATED":
            queue_bootstrap_run(session, row.id)
        return row


ReaderFactory = Callable[[str], GmailBootstrapApiReader]


class GmailBootstrapSource:
    """Resolve a queued GMAIL_TEXT run into one governed HistoryAdapter."""

    def __init__(
        self,
        *,
        settings: Settings,
        runner: GmailProductRunner | None = None,
        reader_factory: ReaderFactory | None = None,
    ):
        self.settings = settings
        self.runner = runner or GmailProductRunner(settings=settings)
        self.reader_factory = reader_factory or (
            lambda token: GmailBootstrapApiReader(
                token_provider=StaticGmailAccessTokenProvider(token),
                timeout_seconds=settings.gmail_bootstrap_timeout_seconds,
            )
        )

    def adapter_for_run(
        self,
        session: Session,
        row: PersonalContextBootstrapRunRow,
        *,
        now: datetime | None = None,
    ) -> GmailBootstrapHistoryAdapter:
        if not self.settings.gmail_bootstrap_enabled:
            raise GmailBootstrapSourceDisabled()
        if row.source_kind != "GMAIL_TEXT":
            raise GmailBootstrapSourceAuthorityInvalid()

        with session.no_autoflush:
            authorization = session.scalar(
                select(ProviderAuthorizationRow).where(
                    ProviderAuthorizationRow.tenant_id == row.tenant_id,
                    ProviderAuthorizationRow.human_identity_id
                    == row.owner_human_identity_id,
                    ProviderAuthorizationRow.provider == "GOOGLE",
                    ProviderAuthorizationRow.product == "GMAIL",
                    ProviderAuthorizationRow.status == "ACTIVE",
                    ProviderAuthorizationRow.revoked_at.is_(None),
                )
            )
        if authorization is None:
            raise GmailBootstrapSourceAuthorityInvalid()

        try:
            acquired = acquire_gmail_slot(
                session,
                authorization.slot_key,
                wait=False,
            )
        except Exception:
            raise GmailBootstrapSourceBusy() from None
        if not acquired:
            raise GmailBootstrapSourceBusy()

        try:
            locked_authorization = session.scalar(
                select(ProviderAuthorizationRow)
                .where(ProviderAuthorizationRow.id == authorization.id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        except Exception:
            raise GmailBootstrapSourceBusy() from None
        if locked_authorization is None:
            raise GmailBootstrapSourceAuthorityInvalid()

        current = now or datetime.now(UTC)
        try:
            governed, binding, refresh_token, _ingress_bearer = (
                self.runner._load_governed_authorization(
                    session,
                    authorization.id,
                    now=self.runner._utc(current),
                )
            )
        except (
            GmailProductAuthorizationUnavailable,
            GmailProductAuthorizationInvalid,
        ):
            raise GmailBootstrapSourceAuthorityInvalid() from None

        # The bootstrap source does not need the neutral-ingress bearer, but
        # reusing the governed loader proves that the persisted installation,
        # binding, credential digest and encrypted secret envelope still agree.
        del _ingress_bearer

        if (
            governed.tenant_id != row.tenant_id
            or governed.human_identity_id != row.owner_human_identity_id
            or gmail_authorization_scope(governed.granted_scopes)
            != GMAIL_READONLY_SCOPE
            or binding.account_key != row.source_account
        ):
            raise GmailBootstrapSourceAuthorityInvalid()

        token = _sanitized(
            GmailProductRefreshFailed,
            lambda: self.runner._refresh_access_token(
                refresh_token,
                GMAIL_READONLY_SCOPE,
            ),
        )
        if not _bearer_token(token):
            raise GmailBootstrapSourceAuthorityInvalid()

        try:
            reader = self.reader_factory(token)
            account_email = reader.profile_email_address()
        except Exception:
            raise GmailProductProviderUnavailable() from None

        account_hash = hashlib.sha256(
            account_email.encode("utf-8")
        ).hexdigest()
        if account_hash != governed.provider_account_hash:
            raise GmailBootstrapSourceAuthorityInvalid()

        return GmailBootstrapHistoryAdapter(
            reader=reader,
            account_email=account_email,
            snapshot_limit=self.settings.gmail_bootstrap_snapshot_limit,
            max_body_bytes=self.settings.gmail_bootstrap_max_body_bytes,
            max_mime_depth=self.settings.gmail_bootstrap_max_mime_depth,
        )


__all__ = [
    "GmailBootstrapBindingInvalid",
    "GmailBootstrapConnectionRequired",
    "GmailBootstrapProductDisabled",
    "GmailBootstrapProductService",
    "GmailBootstrapReadonlyRequired",
    "GmailBootstrapSource",
    "GmailBootstrapSourceAuthorityInvalid",
    "GmailBootstrapSourceBusy",
    "GmailBootstrapSourceDisabled",
    "GmailBootstrapSourceError",
]
