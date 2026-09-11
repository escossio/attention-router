"""Synthetic, offline authorization proof. No production registry or HTTP server."""

import copy
import json
import secrets
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from attention_router.integrations.tenant_binding import (
    INBOUND_SCOPE,
    BindingCode,
    CredentialRecord,
    IntegrationBinding,
    RegistryUnavailable,
    TenantRecord,
    check_inbound_binding,
    credential_digest,
)


NOW = datetime(2026, 9, 11, 20, tzinfo=timezone.utc)
A = "00000000-0000-4000-8000-000000000001"
B = "00000000-0000-4000-8000-000000000002"
SCOPE = frozenset({INBOUND_SCOPE})


class MemoryRegistry:
    """Mutable test double to expose reads; never shipped as a registry adapter."""

    def __init__(self):
        self.credentials = {}
        self.bindings = {}
        self.tenants = {A: TenantRecord(A, True), B: TenantRecord(B, True)}
        self.reads = []
        self.fail_at = None

    def _read(self, operation, key, records):
        self.reads.append((operation, key))
        if self.fail_at == operation:
            raise RegistryUnavailable("private diagnostic must not reach the decision")
        return records.get(key)

    def credential_by_digest(self, digest):
        return self._read("credential", digest, self.credentials)

    def binding_by_id(self, binding_id):
        return self._read("binding", binding_id, self.bindings)

    def tenant_by_id(self, tenant_id):
        return self._read("tenant", tenant_id, self.tenants)


def event_for(binding):
    return {
        "contract_type": "inbound_event", "schema_version": "1",
        "tenant_id": binding.tenant_id,
        "source": {"kind": binding.kind, "name": binding.name,
                   "instance_id": binding.instance_id, "account_id": binding.account_id},
        "external_event_id": "event-1", "event_type": "message",
        "payload_type": "REFERENCE", "payload_ref": {}, "artifact_ids": [],
        "occurred_at": NOW.isoformat(), "received_at": NOW.isoformat(),
        "idempotency_key": "event-1", "correlation_id": "corr-1",
    }


@pytest.fixture
def world():
    registry = MemoryRegistry()
    tokens = {}
    variants = [
        ("a-mail", A, "CHANNEL", "channel.email", "mailbox-1", "account-1"),
        ("a-account", A, "CHANNEL", "channel.email", "mailbox-1", "account-2"),
        ("a-instance", A, "CHANNEL", "channel.email", "mailbox-2", "account-1"),
        ("a-wa", A, "CHANNEL", "channel.whatsapp", "wa-1", "account-1"),
        ("b-mail", B, "CHANNEL", "channel.email", "mailbox-1", "account-1"),
        ("a-cap", A, "CAPABILITY", "capability.calendar", "calendar-1", None),
    ]
    for identity, tenant, kind, name, instance, account in variants:
        binding = IntegrationBinding(identity, "synthetic-ingress", tenant, kind,
                                     name, instance, account, True, SCOPE)
        # Ephemeral test material only, never printed or committed as credentials.
        token = secrets.token_urlsafe(32)
        digest = credential_digest(token)
        tokens[identity] = token
        registry.bindings[identity] = binding
        registry.credentials[digest] = CredentialRecord(
            identity, digest, identity, NOW - timedelta(hours=1),
            NOW + timedelta(hours=1), False, SCOPE,
        )
    return registry, tokens


def decide(world, event=None, identity="a-mail", **kwargs):
    registry, tokens = world
    if event is None:
        event = event_for(registry.bindings[identity])
    return check_inbound_binding(tokens[identity], event, registry=registry,
                                 audience=kwargs.get("audience", "synthetic-ingress"),
                                 now=kwargs.get("now", NOW))


@pytest.mark.parametrize("credential", ["a-mail", "a-account", "a-instance", "a-wa", "b-mail", "a-cap"])
@pytest.mark.parametrize("event_binding", ["a-mail", "a-account", "a-instance", "a-wa", "b-mail", "a-cap"])
def test_only_own_tenant_integration_instance_and_account_are_bound(world, credential, event_binding):
    registry, _ = world
    event = event_for(registry.bindings[event_binding])
    original = copy.deepcopy(event)
    decision = decide(world, event, credential)
    if credential == event_binding:
        assert decision.code == BindingCode.BOUND
        binding = registry.bindings[credential]
        for field in ("binding_id", "audience", "tenant_id", "kind", "name", "instance_id", "account_id"):
            assert getattr(decision.context, field) == getattr(binding, field)
        assert registry.reads[-1] == ("tenant", registry.bindings[credential].tenant_id)
    else:
        assert decision.code == BindingCode.BINDING_FORBIDDEN
        assert decision.context is None
        assert not any(operation == "tenant" for operation, _ in registry.reads)
    assert event == original


def test_positive_events_are_valid_existing_wire_contracts(world):
    schema = json.loads((Path(__file__).resolve().parents[1] /
                         "contracts/integration/v1/integration-contract.schema.json").read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for binding in world[0].bindings.values():
        validator.validate(event_for(binding))


def test_bound_is_not_a_full_wire_validation_or_admission(world):
    event = event_for(world[0].bindings["a-mail"])
    del event["correlation_id"]
    event["unexpected"] = "still requires full schema validation"
    result = decide(world, event)
    assert result.code == BindingCode.BOUND
    assert set(result.__slots__) == {"code", "context"}
    schema = json.loads((Path(__file__).resolve().parents[1] /
                         "contracts/integration/v1/integration-contract.schema.json").read_text())
    assert not Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(event)


@pytest.mark.parametrize("field,value", [
    ("tenant_id", B), ("tenant_id", A + " "),
    ("name", "channel.telegram"), ("kind", "CAPABILITY"),
    ("instance_id", "mailbox-1 "), ("instance_id", "MAILBOX-1"),
    ("account_id", "account-1 "), ("account_id", "ACCOUNT-1"),
    ("account_id", None),
])
def test_valid_shape_mismatches_are_never_normalized(world, field, value):
    event = event_for(world[0].bindings["a-mail"])
    target = event if field == "tenant_id" else event["source"]
    target[field] = value
    assert decide(world, event).code == BindingCode.BINDING_FORBIDDEN


@pytest.mark.parametrize("field", ["tenant_id", "source", "contract_type", "schema_version"])
def test_missing_security_fields_never_receive_defaults(world, field):
    event = event_for(world[0].bindings["a-mail"])
    del event[field]
    assert decide(world, event).code == BindingCode.INVALID_CONTRACT


@pytest.mark.parametrize("field,value", [
    ("tenant_id", None), ("tenant_id", 1), ("tenant_id", ""),
    ("source", []), ("schema_version", 1), ("schema_version", "2"),
    ("contract_type", "channel_delivery"), ("contract_type", "capability_invocation"),
    ("contract_type", "artifact_receipt"), ("contract_type", "integration_result"),
])
def test_malformed_claims_and_other_contract_families_fail_closed(world, field, value):
    event = event_for(world[0].bindings["a-mail"])
    event[field] = value
    assert decide(world, event).code == BindingCode.INVALID_CONTRACT


@pytest.mark.parametrize("field,value", [
    ("kind", []), ("name", "Channel.Email"), ("name", "channel.email\n"),
    ("instance_id", " "), ("account_id", ""), ("account_id", 1), ("extra", True),
])
def test_malformed_source_is_not_native_normalized(world, field, value):
    event = event_for(world[0].bindings["a-mail"])
    event["source"][field] = value
    assert decide(world, event).code == BindingCode.INVALID_CONTRACT


def test_accountless_is_not_a_wildcard(world):
    registry, _ = world
    event = event_for(registry.bindings["a-cap"])
    assert decide(world, event, "a-cap").code == BindingCode.BOUND
    del event["source"]["account_id"]
    assert decide(world, event, "a-cap").code == BindingCode.BOUND
    event["source"]["account_id"] = "account-1"
    assert decide(world, event, "a-cap").code == BindingCode.BINDING_FORBIDDEN
    event = event_for(registry.bindings["a-mail"])
    del event["source"]["account_id"]
    assert decide(world, event).code == BindingCode.BINDING_FORBIDDEN


@pytest.mark.parametrize("secret", [None, "", " ", "short", "x" * 513, "é" * 43])
def test_bad_credential_never_reads_registry_or_event(world, secret):
    registry, _ = world
    decision = check_inbound_binding(secret, object(), registry=registry,
                                     audience="synthetic-ingress", now=NOW)
    assert decision.code == BindingCode.UNAUTHENTICATED
    assert decision.context is None
    assert registry.reads == []


def test_unknown_credential_stops_before_binding_or_payload(world):
    registry, _ = world
    decision = check_inbound_binding(secrets.token_urlsafe(32), object(), registry=registry,
                                     audience="synthetic-ingress", now=NOW)
    assert decision.code == BindingCode.UNAUTHENTICATED
    assert [operation for operation, _ in registry.reads] == ["credential"]


@pytest.mark.parametrize("change", ["revoked", "expired", "future", "wrong-audience"])
def test_credential_lifecycle_precedes_payload_checks(world, change):
    registry, tokens = world
    digest = credential_digest(tokens["a-mail"])
    record = registry.credentials[digest]
    if change == "revoked":
        registry.credentials[digest] = replace(record, revoked=True)
    elif change == "expired":
        registry.credentials[digest] = replace(record, expires_at=NOW)
    elif change == "future":
        registry.credentials[digest] = replace(record, not_before=NOW + timedelta(seconds=1))
    else:
        registry.bindings["a-mail"] = replace(registry.bindings["a-mail"], audience="other-ingress")
    assert decide(world, object()).code == BindingCode.UNAUTHENTICATED
    assert not any(operation == "tenant" for operation, _ in registry.reads)


def test_activation_inclusive_expiry_exclusive_and_timezone_aware(world):
    record = next(iter(world[0].credentials.values()))
    assert decide(world, now=record.not_before).code == BindingCode.BOUND
    assert decide(world, now=record.expires_at).code == BindingCode.UNAUTHENTICATED
    assert decide(world, now=NOW.astimezone(timezone(timedelta(hours=-3)))).code == BindingCode.BOUND
    assert decide(world, now=NOW.replace(tzinfo=None)).code == BindingCode.INGRESS_UNAVAILABLE
    assert decide(world, audience="").code == BindingCode.INGRESS_UNAVAILABLE


@pytest.mark.parametrize("change", ["credential-scope", "binding-scope", "disabled", "tenant", "missing-tenant"])
def test_permission_requires_both_scopes_and_active_binding_and_tenant(world, change):
    registry, tokens = world
    if change == "credential-scope":
        digest = credential_digest(tokens["a-mail"])
        registry.credentials[digest] = replace(registry.credentials[digest], scopes=frozenset({"admin"}))
    elif change == "binding-scope":
        registry.bindings["a-mail"] = replace(registry.bindings["a-mail"], scopes=frozenset())
    elif change == "disabled":
        registry.bindings["a-mail"] = replace(registry.bindings["a-mail"], active=False)
    elif change == "tenant":
        registry.tenants[A] = TenantRecord(A, False)
    else:
        del registry.tenants[A]
    result = decide(world)
    assert result.code == BindingCode.BINDING_FORBIDDEN
    assert result.context is None


@pytest.mark.parametrize("operation", ["credential", "binding", "tenant"])
def test_registry_failures_have_no_success_or_private_diagnostics(world, operation):
    world[0].fail_at = operation
    result = decide(world)
    assert result.code == BindingCode.INGRESS_UNAVAILABLE
    assert result.context is None
    assert "private diagnostic" not in repr(result)


@pytest.mark.parametrize("record", ["credential", "binding", "tenant"])
def test_wrong_registry_lookup_result_is_not_authority(world, record):
    registry, tokens = world
    if record == "credential":
        registry.credentials[credential_digest(tokens["a-mail"])] = registry.credentials[
            credential_digest(tokens["b-mail"])]
    elif record == "binding":
        registry.bindings["a-mail"] = registry.bindings["b-mail"]
    else:
        registry.tenants[A] = registry.tenants[B]
    event = event_for(registry.bindings["a-mail"])
    assert decide(world, event).code == BindingCode.INGRESS_UNAVAILABLE


def test_rotation_preserves_binding_and_revocation_is_read_on_next_call(world):
    registry, tokens = world
    event = event_for(registry.bindings["a-mail"])
    original = decide(world, event)
    old_digest = credential_digest(tokens["a-mail"])
    token = secrets.token_urlsafe(32)
    digest = credential_digest(token)
    registry.credentials[digest] = replace(registry.credentials[old_digest],
                                           credential_id="rotated", digest=digest)
    tokens["rotated"] = token
    rotated = decide(world, event, "rotated")
    assert replace(rotated.context, credential_id=original.context.credential_id) == original.context
    assert rotated.context.credential_id != original.context.credential_id
    registry.credentials[old_digest] = replace(registry.credentials[old_digest], revoked=True)
    assert decide(world, event).code == BindingCode.UNAUTHENTICATED
    assert decide(world, event, "rotated").code == BindingCode.BOUND
    registry.bindings["a-mail"] = replace(registry.bindings["a-mail"], active=False)
    assert decide(world, event, "rotated").code == BindingCode.BINDING_FORBIDDEN


def test_untrusted_metadata_refs_and_timestamps_never_promote_authority(world):
    registry, tokens = world
    event = event_for(registry.bindings["a-mail"])
    event.update({
        "metadata_sanitized": {"owner_authenticated": True, "from_me": True,
                               "approved": True, "tenant_id": B, "scopes": ["admin"]},
        "actor": {"external_actor_id": "guessed-owner"},
        "artifact_ids": ["foreign-artifact"], "payload_ref": {"url": "https://example.invalid"},
        "received_at": "1900-01-01T00:00:00Z",
    })
    result = decide(world, event)
    assert result.code == BindingCode.BOUND
    assert set(result.context.__slots__) == {
        "credential_id", "binding_id", "audience", "tenant_id", "kind", "name",
        "instance_id", "account_id",
    }
    assert result.context.tenant_id == A
    assert tokens["a-mail"] not in repr(result)
    record = registry.credentials[credential_digest(tokens["a-mail"])]
    assert record.digest not in repr(record)
    assert record.digest not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.context.tenant_id = B


def test_binding_permissions_do_not_leak_into_returned_identity(world):
    registry, _ = world
    registry.bindings["a-mail"] = replace(registry.bindings["a-mail"],
                                          scopes=SCOPE | frozenset({"admin", "execute"}))
    result = decide(world)
    assert result.code == BindingCode.BOUND
    assert "scopes" not in result.context.__slots__
    assert "admin" not in repr(result) and "execute" not in repr(result)


def test_unicode_account_identity_is_exact_not_normalized(world):
    registry, _ = world
    registry.bindings["a-mail"] = replace(registry.bindings["a-mail"], account_id="caf\u00e9")
    event = event_for(registry.bindings["a-mail"])
    assert decide(world, event).code == BindingCode.BOUND
    event["source"]["account_id"] = "cafe\u0301"
    assert decide(world, event).code == BindingCode.BINDING_FORBIDDEN


@pytest.mark.parametrize("operation", ["credential", "binding", "tenant", "missing-binding"])
def test_corrupt_registry_records_never_produce_context(world, operation):
    registry, tokens = world
    event = event_for(registry.bindings["a-mail"])
    if operation == "credential":
        registry.credentials[credential_digest(tokens["a-mail"])] = object()
    elif operation == "binding":
        registry.bindings["a-mail"] = object()
    elif operation == "tenant":
        registry.tenants[A] = object()
    else:
        del registry.bindings["a-mail"]
    result = decide(world, event)
    assert result.code == BindingCode.INGRESS_UNAVAILABLE
    assert result.context is None


@pytest.mark.parametrize("changes", [
    {"active": "true"}, {"tenant_id": ""}, {"kind": "UNKNOWN"},
    {"name": "Channel.Email"}, {"scopes": {INBOUND_SCOPE}}, {"scopes": frozenset({"*"})},
])
def test_malformed_server_binding_configuration_is_rejected(world, changes):
    with pytest.raises(ValueError, match="Invalid integration binding record"):
        replace(world[0].bindings["a-mail"], **changes)


@pytest.mark.parametrize("changes", [
    {"revoked": "false"}, {"expires_at": NOW.replace(tzinfo=None)},
    {"expires_at": NOW - timedelta(days=1)}, {"digest": "not-a-digest"},
    {"scopes": frozenset({"integration:*"})},
])
def test_malformed_server_credential_configuration_is_rejected(world, changes):
    with pytest.raises(ValueError, match="Invalid credential record"):
        replace(next(iter(world[0].credentials.values())), **changes)
