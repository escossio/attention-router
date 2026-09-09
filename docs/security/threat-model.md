# Public Threat Model

This is a bounded, current-state model of the documented runtime. It records
controls that exist in the repository and does not claim controls that are
only planned.

## Assets

- inbound events, contact identity and tenant/contact isolation;
- policy versions, decisions, approvals and audit evidence;
- recent context and persistent memory;
- outbox intents, provider credentials and delivery status;
- source, CI workflows, container image, SBOM and attestations.

## Trust boundaries and entry points

Inbound transport enters through normalized events. The agent/model is an
untrusted proposal boundary. Policy is the authorization boundary; human
approval is a separate boundary for critical actions. The database is the
durable state boundary, and outbox/execution is the side-effect boundary.
External providers (WhatsApp and TTS) are outside the core. Browser transport
navigation has an exact-origin boundary. CI/CD and the registry form the
supply-chain boundary.

Entry points include transport webhooks, admin/control-plane routes, worker
queues, browser navigation, provider callbacks, dependency updates and CI
workflow inputs.

## Threat actors

Attackers sending crafted inbound content, compromised contacts or providers,
malicious dependencies, a compromised build environment, and an accidental or
over-privileged operator are in scope.

## Threat scenarios and mitigations

| Scenario | Existing control | Residual risk |
|---|---|---|
| Prompt injection | Model cannot grant execution authority; policy remains outside it. | Policy/model classification can still be wrong. |
| Unauthorized action or policy bypass | Policy plus explicit execution boundary and approval where required. | A configuration error can authorize too much. |
| Approval bypass | Approval is a distinct state before execution. | Operational misuse of approval credentials remains possible. |
| Replay / duplicate events | Unique inbound identity and idempotency; worker leases/claims. | Provider-specific replay semantics still need monitoring. |
| Forged delivery state | Delivery is separate evidence, not proposal or queue state. | A compromised provider callback could lie. |
| Secret leakage | Secret scanning and provider credentials outside fixtures/docs. | Runtime secret handling and operator endpoints remain sensitive. |
| Malicious dependency | Dependabot, pinned Actions and CodeQL. | Upstream compromise cannot be eliminated. |
| Compromised artifact | Immutable digest, SPDX SBOM and build/ SBOM attestations. | Consumers must verify them. |
| Browser navigation confusion | WHATWG URL parsing and exact trusted origin, fail closed. | Trusted origin compromise is out of scope. |
| Memory poisoning | Eligibility, provenance and privacy documentation separate durable memory. | Incorrect durable input can persist until reviewed/removed. |
| Cross-contact context leakage | Normalized identity and isolation invariants. | Bugs in future adapters or policy configuration remain possible. |

## Residual risks

This prerelease does not establish production-grade provider identity,
enterprise authorization, comprehensive abuse detection, or guaranteed
correctness of model output. External provider compromise, credential theft,
operator error, policy misconfiguration and unsafe future adapters remain
material risks. Users must verify deployment configuration and artifacts.

## Out of scope

Real WhatsApp accounts, live provider operation, production incident response,
enterprise tenancy/compliance certification, physical infrastructure,
cryptographic provider identity, and future roadmap concepts are out of scope.
