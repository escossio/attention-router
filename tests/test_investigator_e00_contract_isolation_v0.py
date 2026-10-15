"""E00: read-only contract compatibility proof; no operational adapter is created.

The existing integration V1 wire surface is intentionally NOT an investigator
ingress. These synthetic tests guard that negative contract and show that
tool permissions are separate from the receipt/tenant identity.
"""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

from attention_router.integrations.admission import _parse
from attention_router.integrations.tenant_binding import (
    INBOUND_SCOPE,
    BindingCode,
    CredentialRecord,
    IntegrationBinding,
    TenantRecord,
    check_inbound_binding,
    credential_digest,
)
from attention_router.investigator.model import Query, SourceEvent
from attention_router.investigator.service import Budget, Investigator
from attention_router.investigator.simulated import (
    MemoryAudit, MemoryStore, ScriptedRuntime, SimulatedConnector,
)


_NOW = datetime(2030, 1, 2, tzinfo=timezone.utc)
_TENANT_A = "synthetic-tenant-a"
_TENANT_B = "synthetic-tenant-b"
_TOKEN = "A" * 43  # Public test fixture value, not an operational credential.


class _SyntheticRegistry:
    """Trusted test double, never a user-supplied registry or live DB."""

    def __init__(self, *, scopes=frozenset({INBOUND_SCOPE})):
        self.binding = IntegrationBinding(
            "synthetic-binding", "synthetic-integration-ingress", _TENANT_A,
            "CAPABILITY", "capability.observer", "synthetic-instance",
            None, True, scopes,
        )
        self.credential = CredentialRecord(
            "synthetic-credential", credential_digest(_TOKEN),
            self.binding.binding_id, _NOW - timedelta(hours=1),
            _NOW + timedelta(hours=1), False, scopes,
        )

    def credential_by_digest(self, digest):
        return self.credential if digest == self.credential.digest else None

    def binding_by_id(self, identity):
        return self.binding if identity == self.binding.binding_id else None

    def tenant_by_id(self, identity):
        return TenantRecord(_TENANT_A, True) if identity == _TENANT_A else None


def _v1_event(registry):
    binding = registry.binding
    return {
        "contract_type": "inbound_event",
        "schema_version": "1",
        "tenant_id": binding.tenant_id,
        "source": {
            "kind": binding.kind,
            "name": binding.name,
            "instance_id": binding.instance_id,
            "account_id": binding.account_id,
        },
        "external_event_id": "synthetic-event-1",
        "event_type": "message",
        "payload_type": "REFERENCE",
        "payload_ref": {},
        "artifact_ids": [],
        "occurred_at": _NOW.isoformat(),
        "received_at": _NOW.isoformat(),
        "idempotency_key": "synthetic-event-1",
        "correlation_id": "synthetic-correlation",
    }


def _decide(registry, event):
    return check_inbound_binding(
        _TOKEN, event, registry=registry,
        audience="synthetic-integration-ingress", now=_NOW,
    )


def test_e00_operational_event_preserves_exact_opaque_original_bytes():
    original = b"\x00\xffsynthetic:pressure\n{not-json}\x00"
    event = SourceEvent(
        "synthetic-zabbix", original, "application/octet-stream",
        _TENANT_A, "synthetic-alert-1",
    )
    assert event.original == original
    assert event.digest == sha256(original).hexdigest()
    assert event.content_type == "application/octet-stream"
    # These DTO fields are claims/data: construction alone NEVER authenticates.
    assert event.tenant_id == _TENANT_A


def test_e00_v1_integration_parser_rejects_native_operational_alert():
    raw = json.dumps({
        "contract_type": "investigator_event",
        "schema_version": "1",
        "tenant_id": _TENANT_A,
        "source": {"kind": "CAPABILITY", "name": "capability.observer",
                   "instance_id": "synthetic-instance"},
        "source_event_id": "synthetic-alert-1",
        "observation": {"severity": "warning"},
    }, separators=(",", ":")).encode()
    event, error = _parse(raw)
    assert event is None
    assert error == "INVALID_CONTRACT"
    # E00 must not masquerade an operational alert as a V1 message.


def test_e00_valid_v1_binding_does_not_accept_operational_contract_family():
    registry = _SyntheticRegistry()
    v1 = _v1_event(registry)
    assert _decide(registry, v1).code == BindingCode.BOUND
    operational = dict(v1, contract_type="investigator_event")
    denied = _decide(registry, operational)
    assert denied.code == BindingCode.INVALID_CONTRACT
    assert denied.context is None


def test_e00_payload_tenant_and_source_are_not_authoritative():
    registry = _SyntheticRegistry()
    claims = _v1_event(registry)
    claims["tenant_id"] = _TENANT_B
    denied = _decide(registry, claims)
    assert denied.code == BindingCode.BINDING_FORBIDDEN
    assert denied.context is None
    claims = _v1_event(registry)
    claims["source"]["instance_id"] = "synthetic-other-instance"
    denied = _decide(registry, claims)
    assert denied.code == BindingCode.BINDING_FORBIDDEN
    assert denied.context is None


def test_e00_proposed_operational_scope_cannot_replace_v1_inbound_permission():
    # The candidate scope is not a deployed capability or an accepted policy.
    proposed_scope = "investigator:observation:read"
    registry = _SyntheticRegistry(scopes=frozenset({proposed_scope}))
    denied = _decide(registry, _v1_event(registry))
    assert denied.code == BindingCode.BINDING_FORBIDDEN
    assert denied.context is None
    assert INBOUND_SCOPE != proposed_scope


def test_e00_v1_binding_grants_no_implicit_investigator_tenant_or_tools():
    registry = _SyntheticRegistry()
    assert _decide(registry, _v1_event(registry)).code == BindingCode.BOUND
    event = SourceEvent(
        "synthetic-zabbix", b"synthetic-observation", "text/plain",
        registry.binding.tenant_id, "synthetic-alert-1",
    )
    connector = SimulatedConnector(
        "zabbix", {"read_event": {"severity": "warning"}},
    )

    # A valid V1 receipt or tenant claim is not a permission to investigate.
    store, audit = MemoryStore(), MemoryAudit()
    blocked = Investigator(
        ScriptedRuntime((Query("zabbix", "read_event"),)),
        {"zabbix": connector}, store, audit, Budget(2, 5),
    ).investigate(event)
    assert blocked.status == "blocked"
    assert blocked.error_code == "TENANT_NOT_AUTHORIZED"
    assert blocked.query_count == 0
    assert blocked.partial_evidence == ()

    # Even explicitly authorizing a tenant does not infer any tool privileges.
    denied = Investigator(
        ScriptedRuntime((Query("zabbix", "read_event"),)),
        {"zabbix": connector}, MemoryStore(), MemoryAudit(), Budget(2, 5),
        authorized_tenants=frozenset({_TENANT_A}),
        allowed_operations={},
    ).investigate(event)
    assert denied.status == "blocked"
    assert denied.error_code == "OPERATION_NOT_ALLOWED"
    assert denied.query_count == 1
    assert denied.partial_evidence == ()
