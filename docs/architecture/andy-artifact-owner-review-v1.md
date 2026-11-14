# Andy Artifact Sensitive Owner Review V1: capture first, authority later

Status: isolated, **feature disabled by default**. Only synthetic tests; no actual card data in source, tests, logs, or GitHub.

## User requirement

The original photo must not be discarded merely because it can contain personal or financial information. Andy should observe it, identify its content and preserve the source Artifact. A separate authenticated owner decision determines whether the derived information may become eligible for later private contextual use.

A human decision about data must not be interpreted as an execution authorization, a WhatsApp reply command, an endorsement that the image is authentic, or permission to share its contents.

This differs from an execution-intent approval. The Android app already has an **Aprovações** panel, backed by a typed client-approval HTTP contract. V1 reuses that **display and transport** contract while storing data-consent decisions in their **own** PostgreSQL table; no change to the Android APK is required for this minimal surface.

## Existing components, retained

- WhatsApp Transport captures an incoming JPEG/PNG/WEBP/GIF and sends a signed media receipt through the existing internal ingress.
- Artifact Plane stores original bytes by tenant and SHA-256, retains its source receipt, and the multimodal Worker stores summary, visual description, extracted text, key facts, model and timestamps.
- All that remains available as source evidence independent of owner approval to use it in a memory or graph.
- Existing Android Client Session authenticates human, device and active tenant. The native Approval panel lists requests and submits APPROVE or DENY with that bearer session.

## New review boundary

- ARTIFACT_SENSITIVE_REVIEW_ENABLED is false by default and must be explicitly configured in the Artifact Worker and Client API. This flag does not expand WhatsApp capture.
- ARTIFACT_SENSITIVE_REVIEW_START_AT is mandatory and UTC offset-aware whenever the feature is enabled; the source event must not precede the fixed watermark. This prevents accidental retrospective review.
- After a READY organic WhatsApp image is understood, a bounded classifier detects signs of payment cards, identification documents, credentials or contact cards in the structured result.
- Detected candidates are idempotently recorded in artifact_sensitive_reviews: tenant, source event, original Artifact ID, understanding ID, category, sole active OWNER, pending state and timestamps. No extracted data, summary, card numbers, personal names or OCR are copied into this table.
- A tenant with no unique active owner receives UNASSIGNED. It cannot leak to other members or be auto-approved.
- The Android inbox receives a static, sanitized preview without displaying recovered content. APPROVE marks APPROVED_PRIVATE (consent to possible future private retrieval). DENY marks ARCHIVE_ONLY (retain evidence, but do not use in contextual memory). Both record human, device and client-session identity.
- No ExecutionIntent, execution authorization, outbox, memory promotion, replay of WhatsApp events or external operation is created. Conflicting decisions fail closed; identical decisions are idempotent.
- When this feature is enabled, **no IMAGE-derived text** is automatically passed into a decision model or reply prompt, including classifier misses and after consent. A separate explicit retrieval/disclosure contract is required. The old decision is not replayed on approval.

## Security and limitations

- **Current Artifact storage is a local tenant-scoped filesystem with restrictive file permissions, not a confirmed encrypted secret vault**. User responsibility does not provide encryption or grant unrestricted disclosure.
- Financial credentials, government IDs and authentication data require reviewed encrypted storage and retention policy before sensitive images can be certified for long-term confidential use. Do not claim payment-card compliance solely because the user approved.
- The classifier is heuristic. A non-detection does not prove the image has no sensitive information. Never enable automatic promotion for unrecognized images on that assumption.
- Approval does not erase the original source. DENY means do not promote to context, not delete source content. A deletion workflow requires its own immutable-blob and backup-safe semantics.
- Pending requests expire after 30 days. An expiration does not authorize disclosure, deletion or memory promotion.
- Provider images, prompts and OCR are untrusted data. Do not put recovered text, card details, names or source identifiers in previews, audit, exceptions, logs or public artifacts.
- Observed visual/text facts must remain distinct from model inference and human confirmation.

## Live and historical tracks

Live continuity: Only newly completed image analyses can create review candidates. Existing signed media ingress, live audio/text capture, memory ingestion flags, agent and owner-command controls are unchanged.

Historical acceleration: intentionally deferred by the owner. See WhatsApp history checkpoint issue #403. No old photos, chats or media are scanned here and no live-capture watermark is rewound.

## Network placement

No new container, VLAN, network, bridge, IP, L2 identity, service port or static address. Existing flow: Transport -> signed internal ingress -> Artifact Worker -> PostgreSQL. Android -> existing authenticated Client API -> PostgreSQL for the review decision. Network access does not imply authorization.

## Certification and rollout guard

Require pure classifier, SQLite approval/decision, legacy execution-approval, PostgreSQL schema and tenant isolation, synthetic Artifact Worker, provenance, security scan, CI and distributed CI. No real payment data, provider calls, production migrations or deploys before validation.

Next frontier: retrieval of owner-approved image knowledge through a private, minimal-disclosure and retention-controlled endpoint. No Android camera or unrelated user interface rebuild in V1.
