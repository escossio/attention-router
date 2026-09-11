"""Offline inbound binding decision; no admission, HTTP, database or execution.

Registry data and the clock/audience come from trusted server code. A BOUND
decision only establishes an integration namespace for this call. It is not a
reusable authorization token, full wire validation, or proof of durable receipt.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol


INBOUND_SCOPE = "integration:inbound_event:write"
_NAME = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
_BEARER = re.compile(r"[A-Za-z0-9._~+/-]+=*")


def _identifier(value: object, maximum: int) -> bool:
    return type(value) is str and 0 < len(value) <= maximum and bool(value.strip())


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


def _scopes(value: object) -> bool:
    return type(value) is frozenset and all(
        _identifier(scope, 120) and "*" not in scope for scope in value
    )


def credential_digest(secret: str) -> str:
    """Hash a server-issued opaque credential; do not log the input or digest.

    Syntax/length checks cannot establish entropy. Provisioning must generate
    at least 32 random bytes, and keep credentials out of repository fixtures.
    """
    if type(secret) is not str or not 43 <= len(secret) <= 512 or not _BEARER.fullmatch(secret):
        raise ValueError("Invalid credential representation")
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class IntegrationBinding:
    binding_id: str
    audience: str
    tenant_id: str
    kind: str
    name: str
    instance_id: str
    account_id: str | None
    active: bool
    scopes: frozenset[str]

    def __post_init__(self) -> None:
        if not (
            _identifier(self.binding_id, 64)
            and _identifier(self.audience, 120)
            and _identifier(self.tenant_id, 64)
            and type(self.kind) is str and self.kind in {"CHANNEL", "CAPABILITY"}
            and _identifier(self.name, 120) and _NAME.fullmatch(self.name)
            and _identifier(self.instance_id, 120)
            and (self.account_id is None or _identifier(self.account_id, 180))
            and type(self.active) is bool and _scopes(self.scopes)
        ):
            raise ValueError("Invalid integration binding record")


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    credential_id: str
    digest: str = field(repr=False)
    binding_id: str
    not_before: datetime
    expires_at: datetime
    revoked: bool
    scopes: frozenset[str]

    def __post_init__(self) -> None:
        if not (
            _identifier(self.credential_id, 64)
            and type(self.digest) is str and re.fullmatch(r"[0-9a-f]{64}", self.digest)
            and _identifier(self.binding_id, 64)
            and _aware(self.not_before) and _aware(self.expires_at)
            and self.expires_at > self.not_before
            and type(self.revoked) is bool and _scopes(self.scopes)
        ):
            raise ValueError("Invalid credential record")


@dataclass(frozen=True, slots=True)
class TenantRecord:
    tenant_id: str
    active: bool

    def __post_init__(self) -> None:
        if not _identifier(self.tenant_id, 64) or type(self.active) is not bool:
            raise ValueError("Invalid tenant record")


class RegistryUnavailable(Exception):
    """Registry adapters translate unavailable/inconsistent state to this error."""


class BindingRegistry(Protocol):
    """Trusted snapshot, NOT a client-supplied registry or a production cache.

    The future database adapter must supply transactionally coherent state and
    serialize admission with revocation. This protocol does not provide locks.
    """

    def credential_by_digest(self, digest: str) -> CredentialRecord | None: ...

    def binding_by_id(self, binding_id: str) -> IntegrationBinding | None: ...

    def tenant_by_id(self, tenant_id: str) -> TenantRecord | None: ...


class BindingCode(StrEnum):
    BOUND = "BOUND"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    BINDING_FORBIDDEN = "BINDING_FORBIDDEN"
    INVALID_CONTRACT = "INVALID_CONTRACT"
    INGRESS_UNAVAILABLE = "INGRESS_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class BoundIntegration:
    """Server-derived identity only; contains no actor, approval or effect grant."""

    credential_id: str
    binding_id: str
    audience: str
    tenant_id: str
    kind: str
    name: str
    instance_id: str
    account_id: str | None


@dataclass(frozen=True, slots=True)
class BindingDecision:
    code: BindingCode
    context: BoundIntegration | None = None


def _matches_claims(event: object, binding: IntegrationBinding) -> BindingCode:
    # Inspect raw JSON security fields, never native normalizers. The receiver
    # must ALSO validate the entire V1 schema, formats and HTTP parsing profile.
    if type(event) is not dict:
        return BindingCode.INVALID_CONTRACT
    source = event.get("source")
    tenant = event.get("tenant_id")
    if not (
        event.get("contract_type") == "inbound_event"
        and event.get("schema_version") == "1"
        and type(tenant) is str and 0 < len(tenant) <= 64
        and type(source) is dict
    ):
        return BindingCode.INVALID_CONTRACT
    if not (
        {"kind", "name", "instance_id"} <= source.keys()
        and source.keys() <= {"kind", "name", "instance_id", "account_id"}
        and type(source["kind"]) is str and source["kind"] in {"CHANNEL", "CAPABILITY"}
        and _identifier(source["name"], 120) and _NAME.fullmatch(source["name"])
        and _identifier(source["instance_id"], 120)
        and (source.get("account_id") is None or _identifier(source["account_id"], 180))
    ):
        return BindingCode.INVALID_CONTRACT
    if (tenant, source["kind"], source["name"], source["instance_id"], source.get("account_id")) != (
        binding.tenant_id, binding.kind, binding.name, binding.instance_id, binding.account_id
    ):
        return BindingCode.BINDING_FORBIDDEN
    return BindingCode.BOUND


def check_inbound_binding(
    secret: str | None,
    event: object,
    *,
    registry: BindingRegistry,
    audience: str,
    now: datetime,
) -> BindingDecision:
    """Check current credentials before inspecting event claims; never admit work.

    Pass an explicit server clock and deployment audience, not event timestamps
    or Host/forwarded headers. No positive decision is cached between calls.
    Registry implementations are trusted code and must wrap operational errors
    in RegistryUnavailable; unexpected programming errors propagate (no success).
    """
    if not _aware(now) or not _identifier(audience, 120):
        return BindingDecision(BindingCode.INGRESS_UNAVAILABLE)
    try:
        digest = credential_digest(secret)
    except ValueError:
        return BindingDecision(BindingCode.UNAUTHENTICATED)
    try:
        credential = registry.credential_by_digest(digest)
        if credential is None:
            return BindingDecision(BindingCode.UNAUTHENTICATED)
        if type(credential) is not CredentialRecord:
            return BindingDecision(BindingCode.INGRESS_UNAVAILABLE)
        if not hmac.compare_digest(credential.digest, digest):
            return BindingDecision(BindingCode.INGRESS_UNAVAILABLE)
        if credential.revoked or not credential.not_before <= now < credential.expires_at:
            return BindingDecision(BindingCode.UNAUTHENTICATED)
        binding = registry.binding_by_id(credential.binding_id)
        if type(binding) is not IntegrationBinding or binding.binding_id != credential.binding_id:
            return BindingDecision(BindingCode.INGRESS_UNAVAILABLE)
        if binding.audience != audience:
            return BindingDecision(BindingCode.UNAUTHENTICATED)
        if not binding.active or INBOUND_SCOPE not in credential.scopes & binding.scopes:
            return BindingDecision(BindingCode.BINDING_FORBIDDEN)
        code = _matches_claims(event, binding)
        if code != BindingCode.BOUND:
            return BindingDecision(code)
        tenant = registry.tenant_by_id(binding.tenant_id)
        if tenant is None:
            return BindingDecision(BindingCode.BINDING_FORBIDDEN)
        if type(tenant) is not TenantRecord or tenant.tenant_id != binding.tenant_id:
            return BindingDecision(BindingCode.INGRESS_UNAVAILABLE)
        if not tenant.active:
            return BindingDecision(BindingCode.BINDING_FORBIDDEN)
        return BindingDecision(BindingCode.BOUND, BoundIntegration(
            credential_id=credential.credential_id, binding_id=binding.binding_id,
            audience=binding.audience, tenant_id=binding.tenant_id, kind=binding.kind,
            name=binding.name, instance_id=binding.instance_id, account_id=binding.account_id,
        ))
    except RegistryUnavailable:
        return BindingDecision(BindingCode.INGRESS_UNAVAILABLE)
