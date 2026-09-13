# Andy Product Architecture Checkpoint — 2026-09-11 14:50 America/Fortaleza

## Purpose

This document is a handoff checkpoint for starting a new ChatGPT conversation without repeating completed work. Treat it as context, not as a request to redesign the system.

Repository: `escossio/attention-router`

Authority order for project state:

1. runtime/deployment
2. database/migrations
3. Git / merged code
4. reproducible tests and CI
5. this checkpoint and other docs
6. older chat recollections only as clues

Do not commit secrets, real phone numbers, personal contacts/messages, tokens, private payloads, credentials, `.env`, database dumps, session artifacts, internal IPs/endpoints, or production logs to this public repository.

---

## Product north star

> **A Andy não deveria tentar ser o lugar onde tudo acontece. Ela deveria ser o lugar que sabe fazer tudo acontecer.**

The product is not a WhatsApp bot. The intended product is a personal/contextual agent platform with tenant isolation, personal context, contextual retrieval, authority, memory/learning, artifacts/evidence, integrations/capabilities and progressive delegation.

Construction analogy used throughout the design:

- **Visão**: future product direction
- **Parede**: architectural layer
- **Tijolo**: reusable primitive
- **Pintura**: UX/presentation detail

Prefer reusable primitives over one-off features. A good brick solves many scenarios without provider-specific branching in the core.

---

## Core architectural invariants

### Multi-tenancy

A tenant is the logical customer/data boundary. It is an architectural concept implemented technically with tenant-scoped rows, FKs, query scoping and later defense-in-depth such as RLS.

Do **not** create a table/database/schema per user. Use shared tables + `tenant_id` and preserve the ability to partition/shard later.

Production ingress must not silently fall back to a default tenant.

### Personal Context

Each tenant has an isolated, evolvable personal context for the person Andy represents.

Knowledge is **not** authority.

Andy knowing a fact does not grant permission to disclose it or act on it.

### Retrieval

The execution path should retrieve only the small subset of personal context relevant to the current situation, not load the user's entire life into the model.

### Authority

Approval/disclosure is contextual and depends primarily on the requested data/action and situation. Contact/audience class is a secondary condition, not the main model.

The model, ML layer or repeated user behavior must never self-grant execution or disclosure authority.

### Learning

Separate:

- **Execution Plane**: decide and act now.
- **Learning Plane**: observe outcomes and update controlled personal state over time.

Repetition can create a suggestion, not authorization.

### Artifacts

A memory is not a file. A file is a source of truth/evidence from which knowledge can be derived.

Generic artifacts are separate from the historical voice/TTS-specific `MediaArtifactRow`.

Original bytes should live in object storage; PostgreSQL stores canonical identity, hash, provenance, relationships, metadata and authorization state.

### Integrations

Contract-first, SDK-second, adapters-third.

The Attention Router core should not know provider-specific details such as Gmail/WhatsApp/Telegram. Providers translate their native payloads into language-neutral contracts.

Channels and capabilities are distinct architectural roles:

- **Channel**: where events/messages arrive or are delivered.
- **Capability**: an external service Andy invokes to do/read something.

---

## Completed foundation on `main`

### PR #34 — Artifact Plane V0 — merged

Created tenant-scoped canonical artifact registry and receipts.

Key behavior:

- canonical artifact identity by `tenant_id + SHA-256`
- one artifact can have many receipts/origins
- source provenance preserves channel/account/external receipt/sender/received time
- deduplication only inside one tenant
- replay/idempotency behavior
- generic `ResourceRow` for artifacts
- original file bytes remain outside PostgreSQL via opaque storage reference
- migration `0036_artifact_registry_v0`

### PR #35 — Explicit Tenant Runtime V0 — merged

Merged commit: `80f0bdd9dbe6dd78b4302d4bdcc39a581e84d96f`

Hardens authenticated local WhatsApp ingress:

- explicit `tenant_id`
- local transport forwarding fails closed without tenant
- tenant included in signed/HMAC-protected forwarded payload
- ingress validates tenant exists and is ACTIVE
- replay/conflict/media lookups tenant-scoped

Legacy internal seams may still use `DEFAULT_TENANT_ID`; do not assume tenancy hardening is globally finished.

### PR #36 — Personal Context V0 — merged

Merged commit: `847142532a6777f9455f31b00ac20a787f4f5b61`

Introduced read-only tenant-scoped personal context:

- resolves the single represented owner inside a tenant
- allows multiple provider/source aliases when they converge on one canonical actor
- composes existing `MemoryActor` / `MemoryClaim` / `Fact` primitives instead of creating a parallel memory store
- excludes SECRET, inactive and expired claims fail-closed
- preserves confidence, validity and provenance
- missing/ambiguous represented owner fails closed
- knowledge only; no disclosure or execution authority

### PR #37 — Context Retrieval V0 — merged

Merged commit: `e5eaba9edcaedeeb4c5295b6e4e09cb7f392270f`

Introduced bounded deterministic retrieval over Personal Context:

- lexical relevance V0
- PT-BR stopword handling
- small canonical vocabulary bridge (e.g. principal/primary, pagamento/payment, trabalho/work, filial/branch)
- relevance score + matched terms + provenance
- bounded deterministic ordering
- tenant isolation inherited from Personal Context
- secret/inactive/expired knowledge remains excluded
- deliberately no embeddings/vector DB/LLM ranking yet

### PR #38 — Integration Contract V0 — merged

Merged commit: `af840d9c2860f82d4eeffd2e4871202e72245b9d`

Introduced the provider-neutral integration boundary.

Contracts include:

- `InboundIntegrationEvent`
- `ArtifactReceiptContract`
- `ChannelDeliveryContract`
- `CapabilityInvocationContract`
- `IntegrationResultContract`

Security/semantic rules:

- explicit tenant in contract
- provider external identities remain external until tenant-scoped identity resolution
- external IDs are not promoted into prompt-facing canonical metadata
- a well-formed contract never grants authority
- public provider payloads must not be trusted to choose a tenant
- capability grants/approval/execution gates remain separate
- storage references are opaque references, not public access grants

A language-neutral JSON Schema V1 is published at:

`contracts/integration/v1/integration-contract.schema.json`

There is a schema exporter and CI drift test.

This is the basis for future SDKs; do not make the contract exist only as a Python library.

---

## Current active work — PR #39

PR: **#39 — `feat: prove WhatsApp and email integration adapters`**

Branch: `feat/integration-adapter-proof-v0`

Head at checkpoint creation: `6e89b0b332d5a2622f5fb75eea90ee1d630792a0`

Status at checkpoint creation:

- PR open
- mergeable
- CodeQL completed successfully
- Public CI still running at the last check

Scope:

- reference adapter for the current normalized WWEBJS inbound shape
- provider-neutral email adapter
- both terminate at the same `InboundIntegrationEvent`
- email staged attachments emit `ArtifactReceiptContract` and reuse the existing Artifact Plane
- raw provider payloads are forbidden from asserting `tenant_id`
- external actor/thread IDs stay external until tenant-scoped identity resolution
- tests prove core consumption without provider-specific branching

Deliberate non-goals in #39:

- no Gmail/Microsoft OAuth
- no provider credentials
- no live e-mail account
- no polling/webhook runtime
- no outbound email delivery
- no replacement of current production WWEBJS transport
- no SDK package extraction yet

### Immediate next action in a new chat

1. Inspect PR #39 and its current CI state.
2. If all required checks are green, merge #39 using the same protected PR workflow.
3. If a check failed, inspect the failing job and fix the smallest real defect; do not weaken tests merely to obtain green CI.
4. After the adapter proof is green/merged, decide whether the common seam is stable enough to extract the **Andy Integration SDK** (initially likely Python + TypeScript clients generated/implemented against the same JSON Schema), or whether one more adapter/runtime proof is needed.

Do not start live Gmail/OAuth work before the adapter proof is closed and the SDK extraction boundary is consciously chosen.

---

## Intended integration architecture

Conceptually:

`Provider native payload -> provider adapter -> Integration Contract -> identity/artifact/event bridge -> Attention Router core`

For outbound/channel operations:

`Attention Router execution intent -> Integration Contract -> channel adapter -> provider`

For capabilities:

`Context/intent -> CapabilityRequest -> authority/approval/execution gates -> Integration Contract -> capability provider`

The core should not grow `if gmail`, `if whatsapp`, `if telegram` branches.

---

## Artifact / Knowledge Plane direction after integration work

The previously identified bricks remain:

1. Artifact Registry — built V0
2. Artifact Storage / Object Storage seam — not yet built
3. Source Receipt / Provenance — built V0
4. Extraction Pipeline — pending
5. Structured Extraction (XLSX/PDF/etc.) — pending
6. Semantic Index / embeddings — pending
7. Artifact Relationships — partially enabled through existing Resource/Relationship primitives
8. Access Grants / externally shareable views — pending
9. Audit Trail — leverage existing audit primitives, but artifact-specific sharing audit is pending

Important retrieval rule:

- embeddings help identify *which* evidence/document is relevant
- exact metadata answers exact questions such as `received_at`
- spreadsheet numeric/structural answers should come from structured extraction/query, not vector similarity alone

---

## Scaling direction

The logical model is expected to scale to hundreds of thousands/millions of tenants without changing the domain model.

Scale pressure comes from activity and data volume, not the mere number of tenant rows.

Keep designs compatible with:

- indexing by tenant
- partitioning
- object storage
- async workers/queues
- context retrieval rather than full-context loading
- later sharding/tenant routing
- eventual PostgreSQL RLS or equivalent defense-in-depth

Do not build one DB/schema/table per customer.

---

## Raw conversation preservation

PR #33 remains the raw-conversation preservation PR.

It currently contains:

- `docs/conversation-dumps/2026-09-10-andy-product-brainstorm-raw-001.md`
- `docs/conversation-dumps/2026-09-10-andy-product-brainstorm-raw-002.md`
- `docs/conversation-dumps/2026-09-11-andy-product-build-raw-003.md`
- `docs/conversation-dumps/2026-09-11-andy-product-build-raw-004.md`

Those files are intentionally raw source material, not authoritative architecture summaries.

---

## Instructions for the next ChatGPT conversation

Use this checkpoint plus live GitHub state. Do not repeat completed steps. Do not redesign the engine/policies/WhatsApp transport unless a current failure proves that work is necessary. Preserve the distinction between knowledge, retrieval, authority and execution. Keep provider-specific details at the adapter edge. Keep all new work tenant-scoped and safe for a public repository.

Recommended opening request in the new chat:

> Leia integralmente `docs/checkpoints/ANDY_PRODUCT_ARCHITECTURE_CHECKPOINT_20260911_1450.md` no repositório `escossio/attention-router`, trate-o como checkpoint de contexto e retome a partir do estado real do GitHub. Primeiro verifique o PR #39 e o CI atual. Não repita etapas concluídas e não faça redesign fora da próxima fronteira segura.
