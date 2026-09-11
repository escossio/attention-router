/** Synthetic offline wire round-trip; no provider calls or authority grants. */
import {
  type InboundIntegrationEvent,
  parseIntegrationContract,
  serializeIntegrationContract,
} from "@attention-router/integration-sdk";

const event: InboundIntegrationEvent = {
  contract_type: "inbound_event",
  schema_version: "1",
  tenant_id: "00000000-0000-4000-8000-000000000001",
  source: { kind: "CHANNEL", name: "channel.email", instance_id: "synthetic-mail" },
  external_event_id: "synthetic-event",
  event_type: "message",
  payload_type: "TEXT_REFERENCE",
  occurred_at: "2026-09-11T18:00:00Z",
  received_at: "2026-09-11T18:00:01Z",
  idempotency_key: "synthetic-inbound",
  correlation_id: "synthetic-correlation",
};
const received = parseIntegrationContract(serializeIntegrationContract(event));
if (received.contract_type !== "inbound_event" || received.external_event_id !== event.external_event_id) {
  throw new Error("Synthetic inbound round-trip failed");
}
console.log("TypeScript SDK: synthetic inbound round-trip passed");
