# Andy Account-Outbound Unattributed Image & Document Evidence V1

**Status:** isolated implementation candidate. Capture is disabled by default. It does not establish human authorship or confer action, memory, disclosure, or owner-command authority.

## Why this is needed

The existing live conversation archive (PR #406) stores individual WhatsApp image/document message events and links them by canonical media receipt to the original Artifact, its source and any completed visual understanding. Incoming images can be captured. The current Transport intentionally does not download outbound images classified UNKNOWN_FROM_ME, even though the message and thread are observed.

For WhatsApp media, UNKNOWN_FROM_ME is intentional: the outbound-provenance ledger will not infer that a missing media reference was personally sent by the human owner. The missing reference does not prove a manual send. Those media events must remain unattributed even if their bytes can be saved.

## Live-only opt-in

CONVERSATION_CAPTURE_UNATTRIBUTED_OUTBOUND_MEDIA_ENABLED is false by default and requires an explicit tenant, signed ingress forwarding with a nonempty verified local URL and a configured HMAC secret, WhatsApp source/channel and canonical WhatsApp Artifact ingestion enabled. A nonlocal media-notification URL is forbidden for unattributed account media. This Transport-only flag does not change the backend public route, WhatsApp sending, interaction ownership, user commands, STT, text flags, model settings, or existing incoming media behavior.

The Transport may download a newly observed fromMe image or document only when:

- The currently authenticated WhatsApp self identity is valid and matches both the connected account identifier and the observed message sender, using the existing verified PN/LID alias set. Stale or unavailable authentication fails closed.
- The observed message is account-originated (id.fromMe true or a validated serialized true_ form), targets one individual PN/LID conversation in READY state, has media and belongs to the configured tenant. Group, status, newsletter, owner-self-chat, ambiguous peer and wrong-account observations do not qualify.
- Original and final provenances are both UNKNOWN_FROM_ME, never OWNER_COMMAND, OWNER_MANUAL_OUTBOUND_OBSERVED or ROUTER_AUTOMATED_OUTBOUND_OBSERVED. Router outbound provenance must be absent and classifier reason must be UNMATCHED_MEDIA_FROM_ME or UNMATCHED_MEDIA_REFERENCE, never stale authority, missing ledger, ambiguous or competing automated match.
- There is no human attribution: text and caption of the unattributed account observation remain empty. The event carries a bounded classifier reason and fixed marker VERIFIED_ACCOUNT_UNATTRIBUTED.

The existing media downloader persists bytes once in the opaque spool, sends a signed media receipt to the existing internal ingress and the canonical Artifact registry preserves source event, account and SHA-256. The visual Worker can interpret the image; the live reconciler then links its Artifact to the corresponding message.

The ingestion layer independently rechecks UNKNOWN_FROM_ME, stored event metadata, message type, source-account provenance and the capture marker. It refuses automated, self-command and ambiguous claims. For account-originated events, sender_actor_id is NULL because actor_id can be the destination peer, not the human author. Existing incoming and verified manual-owner voice paths are preserved.

## Security and limitations

- Message is account-observed evidence, not an authenticated human instruction, owner confirmation, authorization to disclose or execute, or verified personal fact.
- The classifier cannot prove that every unmatched account-originated item was manually sent by a human. Evidence capture and human attribution are separate.
- Sensitive visual data still depends on the separate owner-consent and private-retrieval boundaries (draft PR #404, issue #405). Never copy OCR, card numbers, visual descriptions or secrets into conversation_messages, transport logs, GitHub or execution intents.
- The existing tenant-scoped Artifact filesystem is not a certified encrypted secret vault. Capture is not evidence of payment-card compliance or unrestricted retention.
- This implementation only captures newly observed images and documents. It cannot recover old UNKNOWN_FROM_ME events whose bytes were never downloaded. WhatsApp Web download failure leaves explicit pending/failed media, without inventing content.
- Media size, MIME, spool and timeout constraints are inherited. Outbound images use the image formats supported by Artifact Understanding. Document supports PDF and image types. No audio or video expansion.

## Live and historical tracks

Live continuity: newly observed outbound-account media passing the opt-in may be stored and linked to the live archive. Source, message ID, send/receive time and tenant gates of PR #406 remain unchanged.

Historical acceleration: deferred per issue #403. Two earlier outgoing image events remain PENDING because their original media was not captured. This release will not retrieve old WhatsApp message IDs or rewind the capture watermark. Any repair of those messages requires a separately authorized, bounded workflow.

## Communication graph and placement

No new process, container, port, network, VLAN, bridge, dedicated IP, L2/L3 identity or static address. Existing flows:
- WhatsApp Browser/Transport -> signed ingress over existing channel: submit source event.
- WhatsApp Browser/Transport -> existing signed media endpoint: submit opaque content reference.
- Existing internal ingress -> tenant-scoped Artifact filesystem and PostgreSQL.
- Existing visual Worker -> same Artifact/PostgreSQL; existing conversation Worker -> PostgreSQL association.

Application authority remains separate from reachability. No network or cross-tenant scope expansion.

## Certification and activation boundary

Synthetic tests must cover direct image/document paths, durable media receipt, no caption leakage, no sender misattribution, automated/ambiguous rejection, wrong authenticated identity, invalid source/peer/timing, disabled flag, download failures, idempotent replays, ingestion-time checks and full synthetic Artifact/Understanding/conversation association. Run CI, distributed PostgreSQL/Transport, CodeQL and security scans.

After validation use a NEW owner-originated photo for live proof. Do not merge the stacked PR or modify production/boot manifests before preserving Transport session and preparing rollback evidence. Sensitive review PR #404 and private retrieval issue #405 remain separate downstream steps.
