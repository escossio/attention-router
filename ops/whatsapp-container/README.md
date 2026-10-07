# WhatsApp runtime container migration candidate

`WHATSAPP_RUNTIME=CONTAINERIZED` is the **target contract**. Deployment phase is
`PRE_CUTOVER`: AGT production still runs the host units. This package must not
be launched against the live profile until the reviewed cutover window.

## Provenance and forensics

- Stage 3.5 (`9b696ff`) proved an authenticated official Chrome profile plus
  post-auth `whatsapp-web.js` attach. Its automated pairing attempts failed;
  the production design therefore keeps the existing official Chrome profile.
- Stage 3.6 (`19d53ae`) introduced the local transport and safe detach from a
  shared browser. Stage 3.8 (`5978987`, private history, merged as `d3aa623`)
  intentionally introduced host Xvfb, Chrome and Transport units after a live
  soak and restart. Public baseline `1d08444` carried those units forward.
- The existing observer came from private commit `295ac62`; its source and
  synthetic tests are copied here unchanged, except that the container
  configuration explicitly disables raw body capture. This diagnostic service
  has no business authority.
- No complete earlier Browser + Transport Compose implementation was found in
  reachable public/private refs, tags, worktrees, stopped containers or Docker
  image names. The old HA transport was host-native; its preserved LocalAuth is
  historical fallback, not the current AGT Chrome session. An August 8
  `wwebjs-pairing-lab` Compose/image did run isolated Chromium + LocalAuth
  without ingress, outbound, CDP or production profile. It reached QR, not
  a certified authenticated production transport. Stage 3.5 subsequently
  found its Chromium/wwebjs pairing path could not complete, while official
  Chrome pairing and post-auth attach worked. The lab uses `--no-sandbox` and
  explicitly disclaims production security suitability; reuse its volume/
  isolation lessons, not its browser/runtime design.
- The transport source at production merge `20fb542` is byte-identical to the
  current branch's `whatsapp-transport-local/` before importing the observer.

## Communication and placement

| Initiator | Destination | Protocol | Required | Authority | Zone |
| --- | --- | --- | --- | --- | --- |
| Chrome | WhatsApp Web | HTTPS/WebSocket | yes | authenticated profile | browser egress VLAN |
| Transport | Browser CDP edge | TCP 9223 via local proxy | yes | private Docker bridge and local-only transport URL validation | internal CDP bridge |
| Observer | Browser CDP | TCP 9223 loopback | operational only | shared browser network namespace | browser container |
| Transport | internal ingress | HTTP | yes | existing HMAC and tenant validation | transport egress VLAN |
| Worker/API | Transport | HTTP 18103 | as currently enabled | existing outbound HMAC/authority gates | transport VLAN |
| Transport | ROC OTLP | HTTP | optional | existing telemetry settings | transport egress VLAN |

Browser and Transport retain separate L2/L3 identities and static addresses
initially because existing VLAN egress, firewall and inbound integrations use
them. Those addresses are supplied by private host config reconciled against
the currently assigned live namespaces. AGT has no formal IPAM or reserved
test addresses for VLANs 210/211; absence from ARP, ping or Docker inventory
does not authorize a new address. The physical macvlan test is
`BLOCKED_BY_NO_RESERVED_TEST_IP` until an authoritative reservation exists.
Git contains synthetic examples only. The additional internal bridge exists
solely for CDP. Both containers are dual-homed for that one control flow; the
bridge is `internal`, neither container has routing capability, and CDP is not
published on the transport VLAN. The Transport's local proxy preserves the
existing localhost-only CDP trust check. There is no host port publication,
LAN route, MikroTik change or host-created netns in the target.
Browser and Transport currently use distinct private resolvers through their
host unit overrides. The Compose environment must supply their corresponding
resolver addresses separately; verify DNS and egress from each VLAN during
the reviewed cutover. The present Browser VLAN interface is a raw VLAN child
and the Transport uses a macvlan base with a VLAN child; Docker macvlan on a
VLAN parent changes that link arrangement and MAC. Its switch/firewall
acceptance remains a physical cutover gate.

## Containers and persistent state

| Component | Image | Persistent mount | Health |
| --- | --- | --- | --- |
| `andy-whatsapp-browser` | pinned official Google Chrome + Xvfb + CDP edge | existing authenticated profile, bind mounted at `/profile` | CDP, Xvfb and edge processes |
| `andy-whatsapp-transport` | pinned Node 22, unchanged transport source + local CDP proxy | existing inbound spool, ledger and media paths | existing `/ready` |
| `andy-whatsapp-observer` | same Node image, observer entrypoint | existing observer output directory | fresh, connected `status.json` |

State classes: authenticated profile, cookies, IndexedDB, service workers,
inbound pending/sending/quarantine/sent, outbound provenance ledger, media,
the Transport's `.wwebjs_cache` in its persistent working directory,
the API/worker's durable historical bootstrap cursors, HMAC environment
references, observer output and release SHA are `MUST_PRESERVE`. The read-only
Transport history endpoint itself has no separate local cursor store. Image
layers, Xvfb display socket and CDP proxy state are
`REGENERABLE`. Chrome `Singleton*` links and PID files are `EPHEMERAL`, but
must be examined only after the old Chrome exits. The retired HA LocalAuth is
`LEGACY` and remains untouched. No secret, session, spool payload or real
inventory is committed.

### Browser profile lifecycle

On Linux, [Chromium's ProcessSingleton](https://chromium.googlesource.com/chromium/src/+/HEAD/chrome/browser/process_singleton_posix.cc)
writes three symlinks in its profile: `SingletonLock` points
to `hostname-PID`, `SingletonSocket` points to a Unix socket under `/tmp`, and
`SingletonCookie` binds that socket to the profile. Chrome refuses to open a
profile when an unreachable lock names a different hostname. The first
container start used a profile with the old host links removed after the host
writer stopped; a subsequent Compose recreate retained links naming the
previous container's hostname and entered a crash loop. A normal Docker
restart can also retain an unbound socket inode in its writable `/tmp` layer.

Compose gives Browser a stable hostname. Before Chrome starts,
`browser-profile-guard.sh` takes a persistent `flock` on the profile's
`.andy-browser-writer.lock`; another cooperating candidate fails closed.
With that lock held, the guard accepts a clean profile or removes exactly the
three known Singleton symlinks only when the lock names this hostname, its PID
is absent in the current namespace, and its socket has no live listener.
Unknown artifacts, a foreign hostname, a live PID or an uncertain socket
block startup. It logs `SINGLETON_STATE=CLEAN`, `ACTIVE`, `STALE_REMOVED` or
`AMBIGUOUS_BLOCKED`. The guard never removes arbitrary profile files. Its
lock does not replace the cutover gate that stops and verifies the old
host-native Browser before the production profile is mounted.

The private stopped-writer profile clone passed first start, Docker restart,
Compose stop/start, force-recreate, down/up and three further recreates with
CDP, health, one writer and zero crash loops. A second candidate mounting the
same clone failed on `flock` before Chrome started. CI repeats the Browser
lifecycle with a synthetic profile and no provider network.

The active profile has four root-owned files (including `Local State` and
Chrome preferences/session metadata) despite its normal runtime UID. The
operator must record their original ownership in the private backup, prove
Chrome can read the staged clone as the container UID, and plan the minimum
ownership correction only after the live writer stops. Do not blindly
`chown -R` the profile. Chrome sandbox behavior under Docker's default
seccomp and dropped capabilities also needs an offline candidate proof. The
candidate vendors the Apache-2.0 [Docker 26.1.5 default seccomp profile](https://github.com/moby/moby/blob/v26.1.5/profiles/seccomp/default.json)
with one additional allow rule for `clone`, `setns`, `unshare` and `chroot`, following
the [Playwright sandbox guidance](https://playwright.dev/docs/docker)
(SHA-256 `4c611e66c0cb4c3450c1c00072e9f16e87daad318989ff70583467149d24cd7f`).
Every other Docker 26.1.5 rule is unchanged. This retains a deny-by-default
syscall profile, dropped Linux capabilities and
`no-new-privileges`. The four broadened sandbox calls require
explicit review. A candidate requiring `--no-sandbox`, `SYS_ADMIN` or
`seccomp=unconfined` is **not certified** by this package.

AGT Docker's `docker-default` AppArmor profile denied Chromium
`userns_create` in an isolated clone test. The versioned
`andy-whatsapp-browser.apparmor` copies the Docker 26.1.5 default template
with only `userns,` added and a browser-specific profile name. Load it with
`apparmor_parser -Kr ops/whatsapp-container/andy-whatsapp-browser.apparmor`
before candidate testing; verify `docker inspect` names that profile. The
profile affects only containers that opt into it. The Transport's Compose
`working_dir` is its existing persistent spool mount so `whatsapp-web.js`
can retain its relative cache while the image root remains read-only.

On AGT, an online clone of the active profile (not a consistent backup) opened
under the pinned Chrome 155 image with this AppArmor profile, dropped
capabilities and the versioned seccomp profile. CDP answered and the clone's
`Local State`, IndexedDB and Local Storage were readable. The isolated test
started at `about:blank` and never contacted WhatsApp Web; authenticated
session reuse is therefore still **unproven**. The test container, internal
network and temporarily loaded AppArmor profile were removed afterward.

## Candidate validation without production takeover

1. Populate a root-only Compose env file from `compose.env.example`, using
   the reconciled live assignments and exact production source/Chrome versions.
   Never commit the filled file. Use synthetic empty state directories and
   `WHATSAPP_START_URL=about:blank` for offline checks.
2. `docker compose --env-file PRIVATE_ENV -f ops/whatsapp-container/compose.yaml --profile production config --quiet`.
3. Build images on the distributed Docker worker/CI gate for the exact PR SHA.
   Verify image labels, architecture, Chrome/Node versions and no embedded
   state or secrets. Run synthetic browser and transport tests offline. With
   `about:blank`, the Transport must remain alive and its local CDP proxy must
   work; the observer must reject the unconnected page with body capture off.
   `/ready`, observer status output and a connected WhatsApp page require the
   authenticated profile and are reserved for the reviewed cutover proof.
4. Validate profile compatibility only with a private, isolated clone or
   snapshot. Never mount the live profile in a candidate while host Chrome is
   active. Never issue pairing, provider calls or outbound delivery in a test.
5. Reconcile live address ownership read-only and verify actual VLAN parent
   semantics. Do not choose test IPs by inference. Physical macvlan proof with
   another IP remains blocked without an authoritative reservation. The live
   namespace addresses may only be released in the reviewed cutover window.

## Cutover runbook — requires separate operator authorization

Preconditions: certified PR head/checks; offline candidate PASS; current
`CONNECTED`/`ready` and QR absent; read-only live IP ownership reconciliation;
an explicit review of the blocked physical test; private complete
configuration backup; spool baseline; tested rollback commands; no second
profile writer; Chrome sandbox validated. Expected interruption is at least
the Chrome/Transport restart interval, with extra time for a consistent profile
snapshot and readiness. No downtime bound is claimed until rehearsal.

1. Freeze the window and record status, spool counts, Chrome version, release
   SHA, profile ownership/locks, container inventory and endpoints privately.
2. Stop only the old Transport, observer and browser, then Xvfb and the CDP
   proxy/netns units in dependency order. Confirm no Chrome writer remains.
3. Take a consistent root-only profile/spool backup **after** the writer stops;
   record ownership, hashes and snapshot ID. Do not delete auth or `Singleton*`
   files merely because they exist; examine stale locks after process proof.
4. Start the Browser container alone on the reserved VLAN with the real bind
   profile. Confirm Chrome/CDP, authenticated page and QR absent. QR means
   immediate rollback, without attempting pairing.
5. Start Transport and observer. Confirm `ready=true`, `CONNECTED`, history
   status, HMAC gates, spool/ledger, owner authority and OTLP, with no duplicate
   inbound. Real outbound requires separate explicit authorization.
6. Soak with restart count, browser/profile stability, backlog, API/worker,
   ROC inventory and resource usage. Only after satisfactory soak stop/disable/
   mask the replaced host units. Retain the root-only rollback bundle.
7. Reboot proof is a later controlled window: Docker restart, authenticated
   browser, Transport `CONNECTED`, no QR, no host-native WhatsApp units active,
   and ROC discovery must all pass before declaring final completion.

### Rollback

If QR appears, CDP is unhealthy, Transport fails to connect, inbound duplicates
or spool/state deviates: stop the three candidate containers; prove no Chrome
container holds the profile; restore the private consistent snapshot only when
required by a demonstrated mutation; restore original ownership; restart the
old netns/proxies, Xvfb, Chrome, observer and Transport in dependency order;
verify `CONNECTED`, no QR, inbound and spool baseline. Never log out or pair.
The old unit definitions and overrides are in the root-only cutover bundle.
Do not mask/delete units until the soak and rollback proof pass.
The versioned `ops/historical/whatsapp-host-native/rollback.sh --dry-run`
checks backup and installed unit availability without changing live state;
its gated `--execute` path is only for the reviewed cutover window.

## Open gates

- Physical Docker macvlan acceptance is `BLOCKED_BY_NO_RESERVED_TEST_IP`:
  no formal IPAM or reserved lab IP exists for either VLAN. Existing live IPs
  are occupied and may be used only after their host writers/netns stop in an
  authorized cutover window. No candidate physical network has been created.
- Chrome sandbox, CDP and profile format opened on an isolated clone with the
  browser-specific AppArmor profile. Authenticated session reuse remains
  unproven; four root-owned live profile files require explicit cutover handling.
- Candidate images were built on a distributed worker and the synthetic
  offline gate passed. Corrective PR checks and Docker/ROC live integration
  proof remain pending.
- Andy Ops and the live preflight still report host units; they must switch to
  container evidence before the host units can be retired.
- The existing observer was configured to capture message bodies. The target
  disables body capture. Existing private logs remain private and untouched.

Live continuity: the same inbound HMAC path, spool and Transport source are
retained; the cutover requires no duplicate inbound. Historical acceleration:
the existing read-only history endpoint and state are retained with no new
bootstrap or cursor change.
