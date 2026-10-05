"""Fail-closed Gmail history cursor handoff across authorized tenants."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.infrastructure.gmail_slot_lock import acquire_gmail_slot
from attention_router.infrastructure.models import (
    IntegrationBindingRow,
    IntegrationCredentialRow,
)
from attention_router.infrastructure.provider_authorization_models import (
    ProviderAuthorizationRow,
)
from attention_router.infrastructure.repository import audit
from attention_router.integrations.gmail_api_reader import history_id
from attention_router.integrations.tenant_binding import INBOUND_SCOPE


class GmailCursorHandoffError(RuntimeError):
    """Stable, non-secret operational refusal."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class GmailCursorHandoffResult:
    source_installation_id: str
    destination_installation_id: str
    state: str
    cursor_copied: bool


def _fail(code: str) -> None:
    raise GmailCursorHandoffError(code)


def _installation_id(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        _fail(f"GMAIL_CURSOR_HANDOFF_{label}_INVALID")
    return value.strip()


def _load_slot_keys(
    session: Session,
    source_installation_id: str,
    destination_installation_id: str,
) -> dict[str, str]:
    with session.no_autoflush:
        rows = session.execute(
            select(
                ProviderAuthorizationRow.id,
                ProviderAuthorizationRow.slot_key,
            ).where(
                ProviderAuthorizationRow.id.in_(
                    (
                        source_installation_id,
                        destination_installation_id,
                    )
                )
            )
        ).all()
    slots = {row.id: row.slot_key for row in rows}
    if set(slots) != {
        source_installation_id,
        destination_installation_id,
    }:
        _fail("GMAIL_CURSOR_HANDOFF_INSTALLATION_NOT_FOUND")
    return slots


def _lock_authorizations(
    session: Session,
    source_installation_id: str,
    destination_installation_id: str,
) -> tuple[ProviderAuthorizationRow, ProviderAuthorizationRow]:
    rows = session.scalars(
        select(ProviderAuthorizationRow)
        .where(
            ProviderAuthorizationRow.id.in_(
                (
                    source_installation_id,
                    destination_installation_id,
                )
            )
        )
        .order_by(ProviderAuthorizationRow.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    indexed = {row.id: row for row in rows}
    try:
        return (
            indexed[source_installation_id],
            indexed[destination_installation_id],
        )
    except KeyError:
        _fail("GMAIL_CURSOR_HANDOFF_INSTALLATION_NOT_FOUND")


def _authority_rows(
    session: Session,
    row: ProviderAuthorizationRow,
) -> tuple[IntegrationBindingRow, IntegrationCredentialRow]:
    binding = session.get(IntegrationBindingRow, row.integration_binding_id)
    credential = session.get(
        IntegrationCredentialRow,
        row.integration_credential_id,
    )
    if binding is None or credential is None:
        _fail("GMAIL_CURSOR_HANDOFF_AUTHORITY_MISSING")
    return binding, credential


def _exact_inbound_scope(value: object) -> bool:
    return (
        type(value) is list
        and len(value) == 1
        and value[0] == INBOUND_SCOPE
    )


def _validate_source(
    source: ProviderAuthorizationRow,
    binding: IntegrationBindingRow,
    credential: IntegrationCredentialRow,
) -> str:
    if (
        source.provider != "GOOGLE"
        or source.product != "GMAIL"
    ):
        _fail("GMAIL_CURSOR_HANDOFF_PROVIDER_INVALID")
    if source.status != "REVOKED" or source.revoked_at is None:
        _fail("GMAIL_CURSOR_HANDOFF_SOURCE_NOT_REVOKED")
    if (
        binding.id != source.integration_binding_id
        or binding.tenant_id != source.tenant_id
        or binding.kind != "CHANNEL"
        or binding.name != "channel.email"
        or binding.account_key
        != f"sha256:{source.provider_account_hash}"
        or binding.active
        or not _exact_inbound_scope(binding.scopes)
        or credential.id != source.integration_credential_id
        or credential.binding_id != binding.id
        or not credential.revoked
        or not _exact_inbound_scope(credential.scopes)
    ):
        _fail("GMAIL_CURSOR_HANDOFF_SOURCE_AUTHORITY_ACTIVE")
    if source.gmail_history_id is None:
        _fail("GMAIL_CURSOR_HANDOFF_SOURCE_CURSOR_MISSING")
    try:
        return history_id(source.gmail_history_id)
    except Exception:
        _fail("GMAIL_CURSOR_HANDOFF_SOURCE_CURSOR_INVALID")


def _validate_destination(
    destination: ProviderAuthorizationRow,
    binding: IntegrationBindingRow,
    credential: IntegrationCredentialRow,
    *,
    now: datetime,
) -> None:
    if (
        destination.provider != "GOOGLE"
        or destination.product != "GMAIL"
    ):
        _fail("GMAIL_CURSOR_HANDOFF_PROVIDER_INVALID")
    if destination.status != "ACTIVE" or destination.revoked_at is not None:
        _fail("GMAIL_CURSOR_HANDOFF_DESTINATION_NOT_ACTIVE")
    if (
        binding.id != destination.integration_binding_id
        or binding.tenant_id != destination.tenant_id
        or binding.kind != "CHANNEL"
        or binding.name != "channel.email"
        or binding.account_key
        != f"sha256:{destination.provider_account_hash}"
        or not binding.active
        or not _exact_inbound_scope(binding.scopes)
        or credential.id != destination.integration_credential_id
        or credential.binding_id != binding.id
        or credential.revoked
        or not _exact_inbound_scope(credential.scopes)
        or credential.not_before is None
        or credential.expires_at is None
        or now < credential.not_before.astimezone(UTC)
        or now >= credential.expires_at.astimezone(UTC)
    ):
        _fail("GMAIL_CURSOR_HANDOFF_DESTINATION_AUTHORITY_INVALID")


def handoff_gmail_history_cursor(
    session: Session,
    *,
    source_installation_id: str,
    destination_installation_id: str,
    apply: bool = False,
    now: datetime | None = None,
) -> GmailCursorHandoffResult:
    """Validate and optionally copy an existing Gmail cursor to a new tenant slot.

    This operation performs no provider I/O and never invents a cursor. Callers
    own the transaction. Both Gmail slots are serialized before row validation.
    """

    source_id = _installation_id(
        source_installation_id,
        label="SOURCE_INSTALLATION",
    )
    destination_id = _installation_id(
        destination_installation_id,
        label="DESTINATION_INSTALLATION",
    )
    if source_id == destination_id:
        _fail("GMAIL_CURSOR_HANDOFF_SAME_INSTALLATION")

    stamp = now or datetime.now(UTC)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        _fail("GMAIL_CURSOR_HANDOFF_CLOCK_INVALID")
    stamp = stamp.astimezone(UTC)

    slots = _load_slot_keys(session, source_id, destination_id)
    for slot in sorted({slots[source_id], slots[destination_id]}):
        try:
            acquire_gmail_slot(session, slot)
        except Exception:
            _fail("GMAIL_CURSOR_HANDOFF_LOCK_UNAVAILABLE")

    source, destination = _lock_authorizations(
        session,
        source_id,
        destination_id,
    )
    if source.slot_key != slots[source_id] or destination.slot_key != slots[destination_id]:
        _fail("GMAIL_CURSOR_HANDOFF_SLOT_CHANGED")
    if source.tenant_id == destination.tenant_id:
        _fail("GMAIL_CURSOR_HANDOFF_SAME_TENANT")
    if source.human_identity_id != destination.human_identity_id:
        _fail("GMAIL_CURSOR_HANDOFF_HUMAN_IDENTITY_MISMATCH")
    if source.provider_account_hash != destination.provider_account_hash:
        _fail("GMAIL_CURSOR_HANDOFF_PROVIDER_ACCOUNT_MISMATCH")

    source_binding, source_credential = _authority_rows(session, source)
    destination_binding, destination_credential = _authority_rows(
        session,
        destination,
    )
    cursor = _validate_source(
        source,
        source_binding,
        source_credential,
    )
    _validate_destination(
        destination,
        destination_binding,
        destination_credential,
        now=stamp,
    )

    if destination.gmail_history_id is not None:
        try:
            existing = history_id(destination.gmail_history_id)
        except Exception:
            _fail("GMAIL_CURSOR_HANDOFF_DESTINATION_CURSOR_INVALID")
        if existing != cursor:
            _fail("GMAIL_CURSOR_HANDOFF_DESTINATION_CURSOR_CONFLICT")
        return GmailCursorHandoffResult(
            source_installation_id=source.id,
            destination_installation_id=destination.id,
            state="ALREADY_APPLIED",
            cursor_copied=False,
        )

    if not apply:
        return GmailCursorHandoffResult(
            source_installation_id=source.id,
            destination_installation_id=destination.id,
            state="READY",
            cursor_copied=False,
        )

    destination.gmail_history_id = cursor
    destination.updated_at = stamp
    audit(
        session,
        None,
        "gmail.cursor_handoff_applied",
        {
            "source_installation_id": source.id,
            "destination_installation_id": destination.id,
            "same_human_identity": True,
            "same_provider_account": True,
            "cursor_copied": True,
        },
        origin="operator_gmail_cursor_handoff",
        tenant_id=destination.tenant_id,
        created_at=stamp,
    )
    session.flush([destination])

    return GmailCursorHandoffResult(
        source_installation_id=source.id,
        destination_installation_id=destination.id,
        state="APPLIED",
        cursor_copied=True,
    )


__all__ = [
    "GmailCursorHandoffError",
    "GmailCursorHandoffResult",
    "handoff_gmail_history_cursor",
]
