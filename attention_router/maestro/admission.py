"""Durable, fail-closed MAESTRO relay admission on the existing PostgreSQL engine.

This writes a transport receipt only, not a ticket, priority, execution grant,
incident lifecycle or investigator dispatch.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from attention_router.infrastructure.models import (
    IntegrationBindingRow,
    IntegrationCredentialRow,
    MaestroRelayReceiptRow,
    TenantRow,
)
from attention_router.integrations.tenant_binding import credential_digest
from attention_router.maestro.relay_contract import (
    RelayContractRejected,
    TrustedRelayBinding,
    validate_relay_envelope,
)

RELAY_WRITE_SCOPE = "maestro:relay:write"
RELAY_AUDIENCE = "maestro-relay-ingress"
RELAY_INTEGRATION_NAME = "ops.zabbix_relay"


@dataclass(frozen=True, slots=True)
class MaestroAdmission:
    code: str
    receipt_id: str | None = None
    occurrence_number: int | None = None


def admit_maestro_relay(
    session_factory,
    token: str | None,
    body: bytes,
    *,
    idempotency_key: str | None,
    audience: str = RELAY_AUDIENCE,
) -> MaestroAdmission:
    """Validate, authenticate and commit before confirming a receipt.

    A dedicated capability scope prevents a general integration client from
    gaining MAESTRO admission merely by possessing a normal inbound-event token.
    Every call, including a duplicate, revalidates the credential under locks.
    """
    try:
        digest = credential_digest(token)
    except ValueError:
        return MaestroAdmission("UNAUTHENTICATED")
    if (type(audience) is not str or not audience or
            type(idempotency_key) is not str or len(idempotency_key) > 240):
        return MaestroAdmission("INVALID_CONTRACT")

    result = MaestroAdmission("UNAVAILABLE")
    try:
        with session_factory.begin() as session:
            # Reuse the existing transaction discipline: PostgreSQL required,
            # explicit statement/lock timeouts, tenant->binding->credential locks.
            if session.get_bind().dialect.name != "postgresql":
                return MaestroAdmission("UNAVAILABLE")
            session.execute(text("SET LOCAL lock_timeout = '2s'"))
            session.execute(text("SET LOCAL statement_timeout = '5s'"))
            locator = session.execute(
                select(IntegrationCredentialRow.id, IntegrationCredentialRow.binding_id)
                .where(IntegrationCredentialRow.digest == digest)
            ).first()
            if locator is None:
                return MaestroAdmission("UNAUTHENTICATED")
            tenant_id = session.scalar(
                select(IntegrationBindingRow.tenant_id)
                .where(IntegrationBindingRow.id == locator.binding_id)
            )
            if tenant_id is None:
                return MaestroAdmission("UNAVAILABLE")
            tenant = session.scalar(
                select(TenantRow).where(TenantRow.id == tenant_id).with_for_update()
            )
            binding = session.scalar(
                select(IntegrationBindingRow)
                .where(IntegrationBindingRow.id == locator.binding_id)
                .with_for_update()
            )
            credential = session.scalar(
                select(IntegrationCredentialRow)
                .where(IntegrationCredentialRow.id == locator.id)
                .with_for_update()
            )
            if tenant is None or binding is None or credential is None:
                return MaestroAdmission("UNAVAILABLE")
            if (binding.tenant_id != tenant.id or
                    credential.binding_id != binding.id or
                    not hmac.compare_digest(credential.digest, digest)):
                return MaestroAdmission("UNAVAILABLE")
            now = session.scalar(text("SELECT clock_timestamp()"))
            if credential.revoked or not credential.not_before <= now < credential.expires_at:
                return MaestroAdmission("UNAUTHENTICATED")
            if binding.audience != audience:
                return MaestroAdmission("UNAUTHENTICATED")
            if tenant.status != "ACTIVE" or not binding.active:
                return MaestroAdmission("FORBIDDEN")
            if (type(credential.scopes) is not list or type(binding.scopes) is not list or
                    any(type(scope) is not str for scope in credential.scopes + binding.scopes)):
                return MaestroAdmission("UNAVAILABLE")
            if RELAY_WRITE_SCOPE not in set(credential.scopes) & set(binding.scopes):
                return MaestroAdmission("FORBIDDEN")
            if binding.kind != "CAPABILITY" or binding.name != RELAY_INTEGRATION_NAME:
                return MaestroAdmission("FORBIDDEN")
            authenticated_binding = TrustedRelayBinding(
                tenant_id=binding.tenant_id,
                source_system="zabbix",
                instance_id=binding.instance_id,
            )
            try:
                event = validate_relay_envelope(
                    body, authenticated_binding=authenticated_binding,
                    idempotency_header=idempotency_key,
                )
            except RelayContractRejected:
                return MaestroAdmission("INVALID_CONTRACT")
            existing = session.scalar(
                select(MaestroRelayReceiptRow)
                .where(
                    MaestroRelayReceiptRow.tenant_id == event.tenant_id,
                    MaestroRelayReceiptRow.source_system == event.source_system,
                    MaestroRelayReceiptRow.instance_id == event.instance_id,
                    MaestroRelayReceiptRow.occurrence_number == event.occurrence_number,
                ).with_for_update()
            )
            if existing:
                if not hmac.compare_digest(existing.body_sha256, event.digest):
                    return MaestroAdmission("CONFLICT")
                result = MaestroAdmission("DUPLICATE", existing.id, event.occurrence_number)
            else:
                receipt_id = str(uuid4())
                session.add(MaestroRelayReceiptRow(
                    id=receipt_id,
                    tenant_id=event.tenant_id,
                    binding_id=binding.id,
                    credential_id=credential.id,
                    source_system=event.source_system,
                    instance_id=event.instance_id,
                    occurrence_number=event.occurrence_number,
                    event_id=event.source_event_id,
                    signal_status=event.status,
                    problem_event_id=event.signal.get("problem_event_id"),
                    signal_json=event.signal,
                    occurred_at=event.occurred_at,
                    admitted_at=now,
                    body_sha256=event.digest,
                ))
                session.flush()
                result = MaestroAdmission("ACCEPTED", receipt_id, event.occurrence_number)
    except IntegrityError:
        # Another transaction may have inserted concurrently or state may
        # be inconsistent. The retry can safely retrieve the committed row.
        return MaestroAdmission("UNAVAILABLE")
    except (SQLAlchemyError, OSError):
        return MaestroAdmission("UNAVAILABLE")
    return result
