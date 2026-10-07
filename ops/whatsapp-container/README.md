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
them. Those addresses are supplied by private, IPAM reconciled host config;
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
the API/worker's durable historical bootstrap cursors, HMAC environment
references, observer output and release SHA are `MUST_PRESERVE`. The read-only
Transport history endpoint itself has no separate local cursor store. Image
layers, Xvfb display socket and CDP proxy state are
`REGENERABLE`. Chrome `Singleton*` links and PID files are `EPHEMERAL`, but
must be examined only after the old Chrome exits. The retired HA LocalAuth is
`LEGACY` and remains untouched. No secret, session, spool payload or real
inventory is committed.

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

## Candidate validation without production takeover

1. Populate a root-only Compose env file from `compose.env.example`, using
   IPAM approved allocations and the exact production source/Chrome versions.
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
5. Verify IPAM and actual VLAN parent semantics. The existing host namespace
   addresses may only be released in the reviewed cutover window.

## Cutover runbook — requires separate operator authorization

Preconditions: certified PR head/checks; offline candidate PASS; current
`CONNECTED`/`ready` and QR absent; IPAM reconciliation PASS; private complete
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

- IPAM reservation/reconciliation for existing identities and Docker network
  parent behavior. No candidate physical network has been created.
- Official Chrome sandbox under Docker default restrictions and profile clone
  compatibility; four root-owned profile files require explicit handling.
- Distributed image build and synthetic offline run, GitHub PR checks, and
  Docker/ROC integration proof remain pending.
- Andy Ops and the live preflight still report host units; they must switch to
  container evidence before the host units can be retired.
- The existing observer was configured to capture message bodies. The target
  disables body capture. Existing private logs remain private and untouched.

Live continuity: the same inbound HMAC path, spool and Transport source are
retained; the cutover requires no duplicate inbound. Historical acceleration:
the existing read-only history endpoint and state are retained with no new
bootstrap or cursor change.
