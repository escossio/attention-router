# Andy and Attention Router: container boot recovery certification (13 November 2026)

## Boundary and observed outcome

A controlled AGT restart was observed after host bootstrap and V3 lifecycle repairs. The operator's pause was preserved. The result proves automated startup and local convergence of the examined components, **not** successful end-to-end WhatsApp message delivery.

- Physical parent link returned by +8.252 s; Browser and Transport VLAN interfaces became operational without manual commands.
- Existing systemd units started autonomously. Bootstrap reported BOOT_PREFLIGHT, BOOT_BROWSER, BOOT_TRANSPORT, BOOT_OBSERVER and BOOT_CONVERGENCE as PASS, the last at +102.139 s (monotonic time from boot).
- The existing V3 reconciler reported status=ok, mode=reconcile during boot. A subsequent read-only --check passed.
- Browser, Transport, Observer, Worker, Client API, database and MAESTRO were healthy; none had additional Docker restarts in that verification.
- Transport reported CONNECTED, authenticated identity matched, QR not seen and no recorded disconnects; these fields do not prove receipt by a human.
- Owner automatic responses remained disabled, observed control revision 65.

The two host-level defects corrected in that maintenance were (a) a browser-auth preflight dependent on an AppState property no longer exposed by the current WhatsApp Web page, and (b) an outdated Worker lifecycle manifest that still pinned pre-audio revisions and Compose bytes.

## Browser auth contract

The Browser CDP gate must require native Socket state CONNECTED. If AppState is available, it also must be CONNECTED; explicit OPENING, CONNECTING and UNPAIRED states fail closed. If the property is unavailable, null alone does **not** authenticate a WhatsApp session.

Overall convergence separately verifies a healthy Browser; one existing Chrome profile writer; its profile lock, namespace and page topology; and a healthy Transport with authenticated identity matched, no QR, an expected page count, ready/CONNECTED state and no recorded disconnect regressions. The Observer must be healthy as well.

The repository's SHA-pinned offline reproducer is scripts/operations/whatsapp_bootstrap_auth_patch.py. The host-native bootstrap itself is not published here because it contains local deployment details. The reproducer checks an exact known source SHA, replaces a single guarded code fragment and verifies the exact expected output SHA; it **never** starts containers or overwrites an existing output. The source script and original installation must be backed up and reviewed separately before a future upgrade.

A patched bootstrap source is not a license to skip other runtime gates. The patch utility cannot certify a later, different WhatsApp Web version without a new review and source hash.

## V3 lifecycle manifest contract

Worker and Transport are independently built/released components. The V3 manifest must pin each one by immutable image ID and its own exact OCI revision, rather than requiring the two SHA values to be equal.

The controller must still validate, fail closed, all existing requirements: six pinned Compose inputs, rendered configuration, critical environment fields, protected file ownership and permissions, source/revision parity for each image, transport identity/scope, worker/database health and runtime parity. This correction must not disable any other authority or scope check.

A new healthy, already-matching runtime returns without Docker state changes. It rechecks pins before returning. An unhealthy state may continue through the previously authorized reconciliation path; unexpected drift is rejected before a Docker write. Source and target preservation remains required for any actual transition.

**Do not copy the private running manifest, Compose files, container addresses or credentials into GitHub.** Prepare environment-specific manifests offline, verify SHA-256 for each input and preserve prior rollback pins.

## Verification and limitations

- 31 existing V3 synthetic tests and 3 new V3 tests passed after the source change.
- 21 offline bootstrap patch tests passed. The reproducer generated the exact SHA-256 of the previously installed, independently verified host bootstrap repair.
- Ruff and diff checks passed; Docker-free synthetic failures for mismatched transport revision, Compose hash and critical environment still reject.
- A separate read-only runtime audit verified the installed controller's existing 7 core containers, independent immutable Worker/Transport image pins and owner pause.
- Future full CI is required on the published commit SHA. This document is not a claim that a subsequent application deploy or live text/audio delivery was tested.

## Two context tracks

**Live continuity:** no changes to message ingress, tenant scope, STT, context memory, decision policy, the durable outbox, delivery evidence or communication endpoints. This change repairs readiness and reproduction at startup; it does not itself enable responses.

**Historical acceleration:** no import, history scan, catch-up, cursor reset or replay is introduced by this maintenance. Existing read-only history access is unaffected, and historical backlog must never be treated as authority to send.

## Connectivity and network placement

No new containers, VLANs, IPs, subnets, interfaces or host ports are created. The existing service composition, L2/L3 identities and network trust boundaries are unchanged. On boot, the local systemd controller checks the existing Docker socket; its Browser and Transport readiness gates use the already established private control path. Neither network reachability nor CDP visibility confers owner or tenant authority.

## Rollback

Preserve the exact prior root-owned bootstrap, lifecycle package, private manifest, boot defaults and artifact hashes. A rollback must first verify that the earlier bundle matches the intended already-running runtime. Never restore obsolete pins against a new live image by changing hash fields alone. Explicitly fail rather than start or recreate containers if the target composition is inconsistent; do not reset the WhatsApp session, replay ambiguous outbox entries, or disable the owner pause.

**Acceptance gate:** a subsequent reboot test may certify start/recovery at the chosen SHA, but cannot certify voice-to-voice or text-to-text delivery without a separately authorized canary.
