import {
  type InboundIntegrationEvent,
  type IntegrationContractMessage,
  parseIntegrationContract,
} from "@attention-router/integration-sdk";

// These must fail compilation if required wire fields or the discriminated union drift.
// @ts-expect-error all required wire fields must be present
const missing: InboundIntegrationEvent = { contract_type: "inbound_event" };
// @ts-expect-error unknown message family
const unknownFamily: IntegrationContractMessage["contract_type"] = "unknown";

function narrow(text: string): string {
  const message = parseIntegrationContract(text);
  if (message.contract_type === "artifact_receipt") return message.content_sha256;
  if (message.contract_type === "integration_result") return message.reason_code;
  return message.correlation_id;
}
void missing; void unknownFamily; void narrow;
