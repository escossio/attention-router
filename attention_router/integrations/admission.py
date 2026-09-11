"""Internal PostgreSQL admission proof. No HTTP route, worker or engine invocation.

Trusted provisioning uses validated binding/credential records. Every mutation
locks tenant, binding, credential in that order. Admissions serialize per tenant;
this deliberately conservative V0 trades throughput for an auditable boundary.
"""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import or_, select, text
from sqlalchemy.exc import SQLAlchemyError

from attention_router.contracts.integration import integration_contract_json_schema
from attention_router.infrastructure.models import (
    IntegrationBindingRow as BindingRow,
    IntegrationCredentialRow as CredentialRow,
    IntegrationInboxRow as InboxRow,
    TenantRow,
)
from attention_router.integrations.tenant_binding import (
    BindingCode, CredentialRecord, IntegrationBinding, RegistryUnavailable,
    TenantRecord, check_inbound_binding, credential_digest,
)

_VALIDATOR = Draft202012Validator(integration_contract_json_schema(), format_checker=FormatChecker())


@dataclass(frozen=True)
class AdmissionResult:
    code: str
    receipt_id: str | None = None
    admitted_at: datetime | None = None
    correlation_id: str | None = None


def _limits(session):
    if session.get_bind().dialect.name != "postgresql":
        raise RegistryUnavailable("PostgreSQL is required")
    session.execute(text("SET LOCAL lock_timeout = '2s'"))
    session.execute(text("SET LOCAL statement_timeout = '5s'"))


def _lock(session, model, identity):
    return session.scalar(select(model).where(model.id == identity).with_for_update()
                          .execution_options(populate_existing=True))


def _binding_record(row):
    return IntegrationBinding(row.id, row.audience, row.tenant_id, row.kind, row.name,
                              row.instance_id, row.account_key or None, row.active,
                              frozenset(row.scopes))


def _credential_record(row):
    return CredentialRecord(row.id, row.digest, row.binding_id, row.not_before,
                            row.expires_at, row.revoked, frozenset(row.scopes))


class _Snapshot:
    def __init__(self, credential, binding, tenant):
        self.credential = credential
        self.binding = binding
        self.tenant = tenant

    def credential_by_digest(self, digest):
        return self.credential if self.credential.digest == digest else None

    def binding_by_id(self, identity):
        return self.binding if self.binding.binding_id == identity else None

    def tenant_by_id(self, identity):
        return self.tenant if self.tenant and self.tenant.tenant_id == identity else None


def provision_binding(session_factory, binding: IntegrationBinding):
    """Trusted administrative operation; never called from an event claim."""
    with session_factory.begin() as session:
        _limits(session)
        if _lock(session, TenantRow, binding.tenant_id) is None:
            raise ValueError("Unknown tenant")
        session.add(BindingRow(id=binding.binding_id, audience=binding.audience,
                               tenant_id=binding.tenant_id, kind=binding.kind, name=binding.name,
                               instance_id=binding.instance_id, account_key=binding.account_id or "",
                               active=binding.active, scopes=sorted(binding.scopes)))


def _lock_binding(session, binding_id):
    tenant_id = session.scalar(select(BindingRow.tenant_id).where(BindingRow.id == binding_id))
    if tenant_id is None:
        raise RegistryUnavailable("Unknown binding")
    tenant = _lock(session, TenantRow, tenant_id)
    binding = _lock(session, BindingRow, binding_id)
    if tenant is None or binding is None or binding.tenant_id != tenant_id:
        raise RegistryUnavailable("Inconsistent registry")
    return tenant, binding


def provision_credential(session_factory, credential: CredentialRecord):
    """Add a digest-only credential; rotation retains the same binding ID."""
    with session_factory.begin() as session:
        _limits(session)
        _lock_binding(session, credential.binding_id)
        session.add(CredentialRow(id=credential.credential_id, digest=credential.digest,
                                  binding_id=credential.binding_id, not_before=credential.not_before,
                                  expires_at=credential.expires_at, revoked=credential.revoked,
                                  scopes=sorted(credential.scopes)))


def revoke_credential(session_factory, credential_id: str):
    with session_factory.begin() as session:
        _limits(session)
        binding_id = session.scalar(select(CredentialRow.binding_id)
                                    .where(CredentialRow.id == credential_id))
        _lock_binding(session, binding_id)
        credential = _lock(session, CredentialRow, credential_id)
        if credential is None or credential.binding_id != binding_id:
            raise RegistryUnavailable("Inconsistent registry")
        credential.revoked = True


def disable_binding(session_factory, binding_id: str):
    with session_factory.begin() as session:
        _limits(session)
        _, binding = _lock_binding(session, binding_id)
        binding.active = False


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate member")
        result[key] = value
    return result


def _parse(body):
    if type(body) is not bytes:
        return None, "INVALID_REQUEST"
    if len(body) > 65536:
        return None, "BODY_TOO_LARGE"
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_pairs)
        pending = [(value, 1)]
        while pending:
            item, depth = pending.pop()
            if depth > 64 or isinstance(item, float) and not math.isfinite(item):
                raise ValueError("Invalid JSON profile")
            if isinstance(item, dict):
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
        if not _VALIDATOR.is_valid(value):
            return None, "INVALID_CONTRACT"
        return value, None
    except (UnicodeError, ValueError, RecursionError):
        return None, "INVALID_REQUEST"


def _receipt(row, code):
    return AdmissionResult(code, row.id, row.admitted_at, row.correlation_id)


def _admit(session, secret, body, audience, digest, event, parse_error):
    _limits(session)
    # Discovery reads only trusted identities; reread every mutable field under locks.
    found = session.execute(select(CredentialRow.id, CredentialRow.binding_id)
                            .where(CredentialRow.digest == digest)).first()
    if found is None:
        return AdmissionResult("UNAUTHENTICATED")
    tenant, binding = _lock_binding(session, found.binding_id)
    credential = _lock(session, CredentialRow, found.id)
    if credential is None or credential.binding_id != binding.id or credential.digest != digest:
        raise RegistryUnavailable("Inconsistent registry")
    try:
        registry = _Snapshot(_credential_record(credential), _binding_record(binding),
                             TenantRecord(tenant.id, tenant.status == "ACTIVE"))
    except (ValueError, TypeError):
        raise RegistryUnavailable("Invalid registry record") from None
    # now()/CURRENT_TIMESTAMP would be stale after waiting for a lock.
    now = session.scalar(text("SELECT clock_timestamp()"))
    decision = check_inbound_binding(secret, event, registry=registry, audience=audience, now=now)
    if decision.code != BindingCode.BOUND:
        code = parse_error if decision.code == BindingCode.INVALID_CONTRACT else decision.code.value
        return AdmissionResult(code or "INVALID_CONTRACT")
    existing = session.scalars(select(InboxRow).where(
        InboxRow.tenant_id == binding.tenant_id, InboxRow.binding_id == binding.id,
        InboxRow.contract_type == "inbound_event",
        or_(InboxRow.idempotency_key == event["idempotency_key"],
            InboxRow.external_event_id == event["external_event_id"]),
    )).all()
    if existing:
        if len(existing) == 1 and existing[0].raw_body == body:
            return _receipt(existing[0], "DUPLICATE")
        return AdmissionResult("IDEMPOTENCY_CONFLICT")
    row = InboxRow(id=str(uuid4()), tenant_id=binding.tenant_id, binding_id=binding.id,
                   credential_id=credential.id, contract_type="inbound_event",
                   external_event_id=event["external_event_id"],
                   idempotency_key=event["idempotency_key"],
                   body_sha256=hashlib.sha256(body).hexdigest(), raw_body=body,
                   state="PENDING", admitted_at=now, correlation_id=event["correlation_id"])
    session.add(row)
    session.flush()
    return _receipt(row, "ACCEPTED")


def admit_inbound(session_factory, secret, body: bytes, *, audience: str) -> AdmissionResult:
    """Return a receipt only after commit. Errors never contain input or DB diagnostics.

    Parsing is bounded and private; credential failures take precedence over body
    errors. Every retry reauthenticates, even when an inbox receipt already exists.
    """
    try:
        digest = credential_digest(secret)
    except ValueError:
        return AdmissionResult("UNAUTHENTICATED")
    event, parse_error = _parse(body)
    try:
        with session_factory.begin() as session:
            result = _admit(session, secret, body, audience, digest, event, parse_error)
        return result
    except (SQLAlchemyError, RegistryUnavailable):
        return AdmissionResult("INGRESS_UNAVAILABLE")
