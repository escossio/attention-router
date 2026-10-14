# ROC GoAccess container-first

Migrates only the native `attention-goaccess.service` and
`attention-goaccess-web.service`. The shared host Apache proxy is a separate
gateway migration and is **not restarted** in this phase.

## Inputs
- Apache metadata-only access log, bound **read-only** to `/logs` from
  `GOACCESS_LOG_DIR` (can be a shared log-volume in a future gateway stack).
- HTML report, isolated per-deployment at `GOACCESS_REPORT_DIR`, writable
  by UID 10001. No inherited host path is assumed by Compose.
- `GOACCESS_LISTEN_HOST` and mapped HTTP/WebSocket ports and
  `GOACCESS_WS_URL`, describing the public/admin route. WebSockets must
  use the actual browser-accessible address and scheme.
- Both containers run as nonroot, drop all Linux capabilities, have
  read-only root filesystems and no Docker socket.

## Cutover gate
1. Snapshot original unit files, the feed transform and HTML report to a
   private backup on persistent backup storage. **Never truncate or remove**
   Apache logs or old report files during migration.
2. Start at alternative host ports (e.g. 17990/17991), with a distinct report
   directory. Verify HTTP 200 plus WebSocket upgrade, new report processing,
   allowed log directory access, and browser rendering.
3. Build PR and complete mandatory checks. Confirm service owner, logging,
   restart, health, and fallback.
4. Freeze only the two native GoAccess units after pilot certification,
   apply production host ports 7890/7891 (or proxy routes from manifest),
   verify the same page and WebSocket with external clients.
5. Stop and disable only the two old services. Preserve the originals and
   HTML backup until post-cutover soak. Do not touch Apache or WhatsApp.
6. If candidate fails, stop only the candidate containers and restore original
   services to their prior state; validate old HTTP/WebSocket and input feed.

## Limitations
- For current source, Apache is host-native. This is a staged migration;
  portable log sources require containerizing the ingress gateway or an
  explicitly versioned bridge mount.
- The real-time HTML file contains runtime IPs and routes; never commit data.
- Do not expose unauthenticated administrative analytics to the Internet.
