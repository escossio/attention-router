# Artifact Plane V0 — canonical artifact registry

## Purpose

Artifact Plane V0 introduces the first durable primitive for files and other binary evidence that
arrive from different channels but belong to the same tenant-local knowledge space.

The core rule is:

> An artifact is canonical evidence. A receipt records where and when that evidence was observed.

A WhatsApp attachment and an email attachment with identical content can therefore resolve to one
artifact while preserving two independent receipts and their provenance.

## Data model

### `artifacts`

One immutable content identity inside one tenant. The canonical identity is the tuple
`(tenant_id, content_sha256)`.

The row stores metadata required to locate and classify the original object, but not the file bytes:

- tenant scope;
- SHA-256 content identity;
- generic artifact kind and MIME type;
- byte size;
- opaque storage provider/reference;
- optional original filename;
- lifecycle status and metadata;
- a one-to-one `ResourceRow` identity so existing platform relationships, capabilities, facts,
  timeline events, and future grants can address the artifact without creating a parallel entity
  system.

Content is deliberately **not** deduplicated across tenants.

### `artifact_receipts`

One observed delivery of an artifact. A receipt preserves:

- tenant scope;
- source channel and source account;
- source-native receipt/message identifier;
- optional canonical sender actor id;
- exact received timestamp;
- source metadata.

The unique source coordinate is
`(tenant_id, source_channel, source_account, external_receipt_id)`.

Replaying the same source coordinate with the same content is idempotent. Replaying it with a
different content hash fails closed.

## Storage boundary

V0 never stores binary payloads in PostgreSQL. `storage_reference` is an opaque persistent object
reference supplied by an ingestion adapter. It must not contain temporary signed URLs, bearer
credentials, tokens, or other secrets.

Object-store upload/download is intentionally outside this increment. The registry is storage
provider neutral so a later adapter can use S3-compatible storage, cloud object storage, or another
backend without changing artifact identity.

## Tenant boundary

Every registration requires an explicit `tenant_id`; there is no default tenant parameter in the
Artifact Plane API. Deduplication, source idempotency, lookup, and receipts are tenant scoped.

`artifact_receipts` also uses a composite tenant/artifact foreign key so a receipt cannot legally
point at an artifact owned by a different tenant.

## What V0 proves

V0 establishes the structural seam required for the product scenarios discussed during product
brainstorming:

- the same spreadsheet arriving by WhatsApp and email can become one canonical artifact with two
  provenances;
- the original received timestamp remains queryable independently of extracted text or embeddings;
- an artifact already has a platform `ResourceRow`, so later relationships such as branch, employee,
  reporting period, or project can attach to the existing platform ontology;
- later semantic or structured extraction can be derived from the artifact rather than becoming the
  source of truth itself.

## Explicit non-goals

V0 does **not** implement object-store transport, WhatsApp/email attachment ingestion, OCR, vision,
spreadsheet parsing, embeddings/vector search, semantic retrieval, public share links, or user-facing
UI.

Those are later layers on top of the canonical registry, not prerequisites for defining artifact
identity and provenance correctly.

## Next construction boundaries

The intended sequence after this registry is proven is:

1. object-store adapter and ingestion seam;
2. extraction/derivation records for text, tables, images, and metadata;
3. tenant-scoped semantic index and structured retrieval;
4. revocable, expiring access grants backed by existing platform authority primitives;
5. source adapters such as WhatsApp and email;
6. user-facing presentation only after the underlying contracts are stable.

This order keeps the project focused on structural primitives before presentation concerns.

## Artifact Store V1

Issue #153 implements the byte-storage boundary that V0 deliberately left open.
The registry schema is unchanged. Binary payloads still never live in PostgreSQL.

The first backend is a local immutable content-addressed store behind the
provider-neutral ArtifactObjectStore protocol. Its canonical storage metadata is:

- storage_provider=local-fs-v1;
- storage_reference=sha256:<content-sha256>.

The storage reference alone is not a global locator. Resolution always also
requires the artifact's explicit tenant scope. The local backend derives an
opaque SHA-256 namespace from tenant_id, so identical content in two tenants
has the same content reference but different physical paths.

Source filenames, MIME types, sender values and provider identifiers never become
filesystem path components. A local object path is derived only from the trusted
store root, the hashed tenant namespace and the validated content digest.

Writes are immutable and atomic: data is written to a temporary file in the
target shard, flushed with fsync, moved into place with os.replace, and the
directory is then flushed. Existing objects are not trusted merely because a
path exists; size and SHA-256 are revalidated before reuse.

Reads use the persisted ArtifactRow identity and require an explicit tenant.
Only AVAILABLE artifacts may be read. The selected backend must match the
artifact's storage_provider, and every read revalidates storage reference,
expected size and content SHA-256. Symlinks and non-regular final objects fail
closed.

### Staging seam

stage_artifact_receipt() is the application boundary for source adapters:

untrusted bytes
  -> ArtifactObjectStore.put_bytes()
  -> canonical SHA-256 / size / storage reference
  -> register_artifact_receipt()
  -> ArtifactRow + ArtifactReceiptRow

The caller still owns the database transaction. Storage is intentionally written
before registry persistence so a committed Artifact never points at bytes that
were never durably stored. If the database transaction later rolls back, an
unreferenced immutable blob may remain. The staging path must not delete it
inline because another concurrent receipt may already reference the same
content. Garbage collection is a separate future reconciliation concern.

### Configuration and safety

The store is default-off:

| Setting | Default | Contract |
| --- | --- | --- |
| ARTIFACT_STORE_ENABLED | false | Canonical storage remains opt-in even though Gmail and WhatsApp adapters can consume it. |
| ARTIFACT_STORE_ROOT | /var/lib/attention-router/artifacts | Must be absolute when enabled. |
| ARTIFACT_STORE_MAX_BYTES | 33554432 | Per-object bound; configurable from 1 byte to 1 GiB. |
| WHATSAPP_ARTIFACT_INGESTION_ENABLED | false | Enables canonical staging for signed local WhatsApp media notifications. |
| WHATSAPP_ARTIFACT_MAX_BYTES | 33554432 | WhatsApp opaque-media bound; cannot exceed the Artifact Store bound when enabled. |

Storage treats content as opaque bytes. MIME type is classification metadata, not
execution authority. V1 does not parse PDFs, images, spreadsheets, archives or
documents; it does not run OCR, macros, decompression or embedded content.

V1 also does not expose public download/share URLs. The read seam is internal and
tenant-scoped. Public/user presentation and grants remain later layers over the
existing Artifact ResourceRow authority model.

Gmail attachment ingestion is merged and stages provider attachments here before
neutral event admission. WhatsApp inbound media can now use the same canonical
Artifact identity after its signed, explicit-tenant local media notification is
matched to a committed inbound event. The legacy voice MediaArtifactRow remains
in parallel only for transcription compatibility during this increment.

## Artifact Understanding V1

Issue #166 adds a derived understanding layer above the immutable Artifact Store.
The source Artifact remains canonical evidence; model output is a versioned
derivation and never replaces the bytes, receipt provenance or Resource identity.

V1 is intentionally bounded to inbound WhatsApp image/document events whose
canonical Artifact is already AVAILABLE. Supported content types are:

- image/jpeg;
- image/png;
- image/webp;
- image/gif;
- application/pdf.

The application reads bytes only through the tenant-scoped internal Artifact
read seam. It does not expose a public URL and does not execute or locally parse
received content. Images are sent to the multimodal provider as image inputs.
PDFs are sent as file inputs so the provider can use document text plus page
imagery. Raw bytes never enter PostgreSQL.

### Derived record

Each inbound event may own one artifact_understandings row. The row records:

- explicit tenant and inbound-event identity;
- optional canonical Artifact identity;
- PENDING / PROCESSING / READY / FAILED state;
- IMAGE or DOCUMENT content kind;
- bounded summary and extracted/visible text;
- bounded visual description and key facts;
- detected language and whether text extraction was truncated;
- provider, model and prompt-version provenance;
- provider request reference without credentials;
- bounded retry/lease/error metadata.

Replay of the same inbound event is idempotent. A READY understanding for the
same tenant-local Artifact may be reused by another inbound event only when the
provider, model and prompt version also match.

### Decision boundary

When Artifact Understanding is enabled, a supported image/document event may not
reach agent decision-making until its understanding is READY.

The decision queue uses WAITING_ARTIFACT_UNDERSTANDING while the media
notification or provider result is pending. FAILED understanding cancels the
media-dependent decision fail-closed rather than allowing the agent to guess
about unseen content.

READY derived content is appended to the effective inbound text behind an
explicit ANEXO_NAO_CONFIAVEL boundary. Text, prompts, policies, instructions,
links or commands found inside an Artifact remain user-provided data; they do
not gain system, policy, authorization or execution authority.

Historical conversation rendering reuses the same persisted derivation, so later
turns retain what was read without mutating the original inbound event.

### Provider boundary

The first provider is OpenAI Responses and is enabled only with explicit
configuration. Image inputs use bounded base64 data URLs. PDF inputs use bounded
input_file data URLs and high page-image detail. Provider output is accepted
only through a strict structured schema.

No provider credential, raw base64 payload or raw file byte sequence is
persisted as derivation metadata.

### Configuration

Artifact Understanding is default-off:

| Setting | Default |
| --- | --- |
| ARTIFACT_UNDERSTANDING_ENABLED | false |
| ARTIFACT_UNDERSTANDING_PROVIDER | openai |
| ARTIFACT_UNDERSTANDING_MODEL | gpt-5.6-sol |
| ARTIFACT_UNDERSTANDING_MAX_BYTES | 20971520 |
| ARTIFACT_UNDERSTANDING_TIMEOUT_SECONDS | 60 |
| ARTIFACT_UNDERSTANDING_PROCESSING_LEASE_SECONDS | 180 |
| ARTIFACT_UNDERSTANDING_BATCH_SIZE | 10 |
| ARTIFACT_UNDERSTANDING_MAX_OUTPUT_TOKENS | 4096 |

Enabling requires both Artifact Store V1 and OPENAI_API_KEY. The understanding
byte limit may not exceed the configured Artifact Store object limit.
