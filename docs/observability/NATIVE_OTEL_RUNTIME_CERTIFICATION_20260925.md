# Native OpenTelemetry runtime certification — 2026-09-25

Status: PASS. This record covers native Transport → Internal Ingress tracing for a real inbound WhatsApp message.

Runtime Transport source: `fedd841bb660f0ff820ae0ff218d55b18e7a3ec8`, from protected `main` after PR [#191](https://github.com/escossio/attention-router/pull/191) and the Puppeteer ESM attach compatibility correction in [#192](https://github.com/escossio/attention-router/pull/192). All required checks passed before deployment. Internal Ingress retained its separately deployed native tracing release.

## Operational evidence

| Check | Result |
|---|---|
| Transport liveness/readiness, connected client and owner authority | PASS |
| Internal Ingress liveness/readiness and native OTel | PASS |
| Transport native OTel and service version `0.1.0` | PASS |
| Browser/session continuity across Transport replacement | PASS |
| Host OTLP edge rejects unauthorized source and accepts both allowed sources | PASS |
| Real inbound canary observed as delivered and forwarded | PASS |
| Both components exported successfully after the canary | PASS |
| Collector received/exported native spans and Tempo returned them | PASS |
| ROC reconstruction bridge remained healthy and independent | PASS |

Only Transport was restarted for the application rollout. The initial attach failure triggered a successful component-local rollback; the corrected release then passed the same readiness gate. The browser PID remained unchanged across both attempts. No browser, database, Internal Ingress or ROC bridge restart was required.

The existing network path was preserved:

```text
Transport ---------\
                     > host OTLP edge → Collector → Tempo
Internal Ingress --/
```

The independent ROC reconstruction service also returned a recent trace in the canary window. Its container identity, start time and restart count remained unchanged, and no unhealthy or restart event was observed during the rollout.

The edge continues to proxy through host loopback. The Collector remains outside the application network domains.

## Trace graph verified in Tempo

```text
Attempt trace:
transport.receive (root)
└── transport.ingress_attempt
    └── ingress.accept (remote child)

Separate canonical trace:
attention.message (root)
└── Span Link → transport.ingress_attempt in the attempt trace
```

One complete linked chain was validated across two native traces containing five spans. The initial search was empty; repeating the same query and time window after indexing returned the traces without changing runtime configuration.

Parent and trace IDs were compared directly in the retrieved spans: the receive span has no parent, the attempt points to receive, and Ingress continues the same trace with the attempt as its parent. The canonical root has a different trace ID and a Link to that exact attempt span.

`attention.message` carries a valid Andy `roc.correlation_id` UUID, distinct from both OTel trace IDs. The correlation identity was not replaced by a tracing identifier.

All four boundary spans carry `roc.trace_source=native` and `roc.synthetic=false`, with successful delivery/admission outcomes. Resources identify `attention-router-transport` and `attention-router-ingress`, version `0.1.0`, and `deployment.environment=private` where emitted.

## Privacy and evidence handling

The audit inspected the complete retrieved native trace JSONs, including Resources, instrumentation scope metadata, attributes, events, Links and status descriptions. It checked the source-defined key/value allowlists and explicitly searched for payload/text, phone/JID/LID/external actor identifiers, prompts/model output, credentials, tokens, HMAC, cookies, exception messages/stacks, baggage and tracestate. Arbitrary events, Link attributes and private status text were absent. Result: PRIVACY_AUDIT=PASS.

Raw trace/search responses, operational metadata, readiness checks, source provenance, continuity evidence and audit results remain in a root-owned private evidence directory with mode `0700`. This public record includes no real message content, identities, correlation/trace/span IDs, private addresses, environment contents or host inventory.

## Scope and sampling

This certifies the live transport-attempt/admission boundary and its independent canonical root. It does not certify future durable context propagation or downstream worker/provider tracing.

`parentbased_traceidratio` with ratio `1.0` remains the current certification setting. The steady-state sampling policy is a separate operational decision; it was not changed during this milestone.
