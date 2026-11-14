# Andy Live Conversation Artifact Links V1

Status: isolated, unmerged implementation candidate. Foundation for image/document understanding in conversations, not authorization to retrieve secrets.

## Problem and source evidence

The WhatsApp pipeline retains each signed ingress event, canonical Artifact receipt and (for an eligible image/document) Artifact Understanding. The live conversation archive initially recorded only TEXT/VOICE. Thus an incoming photo with a completed semantic description could be absent from its conversation timeline.

The persisted event provides tenant, source account, provider message ID, normalized READY conversation key, timestamp and origin. Never infer links by similarity of OCR or content hash: the same bytes can appear in separate conversations.

## Foundation

- The same archive now accepts IMAGE, DOCUMENT and VIDEO, retaining original captions as untrusted text and preserving the already-certified VOICE transcription behavior. Existing signed ORGANIC origin and fromMe checks, fixed watermark, tenant and conversation-state gates remain in place.
- The message record is created even if the attachment is pending, failed or missing; no missing content is fabricated. Original sent_at, received timestamp, author basis and direction are retained.
- A separate reconciliation checks the original event against the exact tenant, source account, channel.whatsapp and provider-event key of artifact_receipts, then validates the available canonical Artifact and any related Understanding.
- Conversation-message metadata retains only references and states: source_inbound_event_id, attachment_state, artifact_id, artifact_receipt_id, artifact_kind, artifact_understanding_id and artifact_understanding_status. No OCR, financial data, extracted facts or raw image bytes are copied into the conversation row.
- This V1 association is validated by application logic, not by a newly added SQL foreign key. Both canonical Artifact and understanding tables remain the existing relational authority; a separate normalized association table may be justified later if multi-attachment messages are supported.

## Asynchronous completion

The existing bounded Worker reconciliation now has independently limited phases for missing messages, late STT and late Artifact/semantic completion. A photo can reach the database first as an observed message, then be enriched with references after its image receipt and visual understanding are committed. Two messages with identical bytes retain separate provider message IDs and source receipts.

Incomplete media with no available source-backed outcome is excluded from the pending-link query, so it cannot starve later ready files. A missing or conflicting tenant/source account, changed Artifact ID, incompatible media kind or impossible terminal result fails closed. A source-integrity conflict is marked LINK_CONFLICT using only an internal error code, without deleting the observed message or repeatedly occupying the recovery slots. Explicit operator review is needed before a quarantined link can be retried. No model call, notification, new reply, memory claim, execution intent or conversation replay is introduced.

## The user's card example

A future private question such as "qual era o cartão naquela conversa?" requires this bridge from conversation to message to canonical image and derived semantic understanding. This structural link alone does not grant permission to retrieve or disclose the card. The separate draft PR #404 provides a first consent workflow; issue #405 covers authorized private retrieval and crypto/retention controls. Those later changes depend on this conversation link.

## Live and historical

Live continuity is limited to the previously activated CONVERSATION_CAPTURE_ENABLED, exact tenant and CONVERSATION_CAPTURE_START_AT. It can recover post-watermark image messages that already passed the signed ingress but were not archived yet.

Historical acceleration is intentionally deferred under issue #403: no old WhatsApp conversations, photos or media are imported, and no watermark is rewound. Existing Gmail archive is untouched.

## Known limitations and trust boundaries

- Manually sent images/files from the owner's connected account may be observable, but the current Transport does not guarantee that their bytes are downloaded. Keep unresolved authors and pending attachments explicitly pending.
- Video is an opaque linked Artifact, not evidence of video semantic understanding. Stickers and notifications are out of scope.
- The existing Artifact store is tenant scoped with integrity hashes and restrictive file permissions, not proof of an encrypted personal vault.
- Captions are source statements, not necessarily true facts. Visual analysis remains separate from the conversation text and needs explicit private read authority later.
- Large-volume performance on the existing JSON metadata join and additional file varieties require further benchmarking.

## Network and certification

No new database, table, migration, service, IP, VLAN, port, bridge, L2/L3 identity, network attachment or model call. Existing Transport to signed internal ingress to Artifact Worker to PostgreSQL, plus existing live Worker to PostgreSQL only.

Synthetic tests cover out-of-order messages/media, photo captions, PDF, video, duplicate bytes in separate messages, audio STT coexistence, failed media, source-account isolation, source mutation, ambiguous outbound identity, watermark, idempotence and PostgreSQL JSONB reconciliation. Do not merge or deploy before distributed certification and owner validation.
