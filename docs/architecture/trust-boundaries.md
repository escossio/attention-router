# Trust Boundaries

```mermaid
flowchart LR
  T[Inbound transport] --> I[Normalized inbound event]
  subgraph Core[Attention Router core]
    I --> C[Context assembly]
    C --> A[Agent / model proposal]
    A --> P[Policy engine]
    P --> H{Human approval}
    H --> X[Execution / outbox]
    X --> E[Delivery evidence]
    C <--> D[(Database)]
    P <--> D
    H <--> D
    X <--> D
  end
  X --> W[WhatsApp / browser adapter]
  X --> V[External TTS service]
  W --> E
  V --> E
  CI[CI/CD and supply chain] -. builds, scans, attests .-> Artifact[GHCR image + SBOM]
```

The model proposes; policy authorizes; human approval can release critical
work; execution creates effects; delivery evidence records what the external
boundary reports. The database is durable state, not an external-provider
acknowledgement.
