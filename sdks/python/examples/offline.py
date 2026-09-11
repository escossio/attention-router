"""Synthetic offline wire round-trip; no provider calls or authority grants."""

from andy_integration_sdk import InboundIntegrationEvent, parse_contract, serialize_contract

event: InboundIntegrationEvent = {
    "contract_type": "inbound_event",
    "schema_version": "1",
    "tenant_id": "00000000-0000-4000-8000-000000000001",
    "source": {"kind": "CHANNEL", "name": "channel.email", "instance_id": "synthetic-mail"},
    "external_event_id": "synthetic-event",
    "event_type": "message",
    "payload_type": "TEXT_REFERENCE",
    "occurred_at": "2026-09-11T18:00:00Z",
    "received_at": "2026-09-11T18:00:01Z",
    "idempotency_key": "synthetic-inbound",
    "correlation_id": "synthetic-correlation",
}
assert parse_contract(serialize_contract(event)) == event
print("Python SDK: synthetic inbound round-trip passed")
