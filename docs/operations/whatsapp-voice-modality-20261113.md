# WhatsApp voice modality and restart evidence, 13 November 2026

## Scope and authority

This record accompanies PR #398. It distinguishes certified code and boot recovery from **not-yet-certified live message delivery**. Nothing here enables autonomous responses, changes owner control, alters credentials or authorizes a live WhatsApp send. No private host environment or contact information belongs in this repository.

## Implemented behavior and evidence

- d5c414b1eedfa062af864e91ef7fcfce6ff4ef42: pin WhatsApp Web client version 1.34.8; check the media-cache and outgoing media-ID contracts in the image build; ensure image inputs are readable by the non-root transport user.
- 2ced65c3125c6faa633332aa6ed5834aebf9cfa3: mirror eligible visitor voice modality when the explicit flag is enabled, visitor guard and TTS auto-send are configured, and existing authorization succeeds. Text input remains text-only by default.
- The exact-SHA CI on PR #398 reported 12 successful checks, including Python, transport, Docker, secret scanning, CodeQL and four distributed gates.
- Local, isolated validation at that head: 77 voice and visitor Python tests and 288 transport tests passed. Synthetic payloads do not replace a real outbound delivery proof.
- An AGT reboot after the host bootstrap/reconciler repairs demonstrated spontaneous recovery: seven core containers healthy, zero additional container restarts; bootstrap BOOT_PREFLIGHT, BOOT_BROWSER, BOOT_TRANSPORT, BOOT_OBSERVER and BOOT_CONVERGENCE passed. Lifecycle reconciler returned mode=reconcile, status=ok. Transport reported ready, connected and authenticated with no QR and no recorded disconnects.
- The owner-controlled automatic response gate remained paused at revision 65 through that reboot. This record does not certify that a live human received any voice or text.

The host-specific bootstrap and lifecycle remediation are **separate** from PR #398 and require their own versioned patch, tests and operational provenance. Merging #398 alone does not install those host repairs or replace already-running container images.

## Acceptance gates still open

1. Live, authorized **text to text**: capture inbound normalization, policy/authority, the text-only outbox, transport acceptance and delivery evidence.
2. Live, authorized **PTT to PTT**: capture media admission, STT, policy/authority, TTS and Ogg/Opus preparation, a voice outbox, transport acceptance and delivered modality where observable.
3. Keep UNKNOWN_FROM_ME and ambiguous/timeout delivery unconfirmed. No blind replay or accidental conversion of a provider acknowledgement into a human response.
4. Re-evaluate pause, pending effects, access scope, timing and canary recipient before any authorized send. Never remove the owner pause as a side effect of documentation, merge or host restart.

## Two context tracks

**Live continuity:** new authorized events keep using the established inbound normalization, authorization and durable outbox path. Changes in media handling do not reset the ingress cursor, reroute tenant scope, or weaken idempotency and delivery proof.

**Historical acceleration:** no history scan or import was added here. The existing read-only history affordances remain separate and are not a reason to replay old events into the live outbox. Historical ingestion is deferred to its own authorization and idempotency gate.

## Deployment and rollback boundary

Revisions of Client API, Browser, Transport, Worker and MAESTRO are separately pinned by their corresponding images. GitHub merge is not a production deployment. Any image replacement requires explicit manifest/Compose reconciliation, preserved rollback pins and certified no-duplicate delivery semantics. A boot health check is not proof of language-model provider quota, user comprehension or handset playback.
