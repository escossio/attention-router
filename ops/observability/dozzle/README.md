# Dozzle — portable observer with an isolated Docker API proxy

The original `attention-dozzle` service relies on a host Python proxy
(`andy-dozzle-docker-view.service`) and a host bind socket. Replace them
with two OCI services and a Docker-managed Unix socket volume.

**Trust boundary:** the proxy alone mounts the real Docker socket
(which is still powerful even mounted :ro). The proxy has no network,
drops capabilities, permits only GET/HEAD to narrowly listed Dozzle routes,
and rejects mutating verbs and the Docker exec/archive/attach endpoints. The
Dozzle UI receives only the filtered socket. Labels for grouping come from
a private, explicitly provided `DOZZLE_CLASSES_FILE`. Never commit real
classification/private operational inventory or credentials.

The proxy streams logs, so allowlisted GET /containers/:id/logs can expose
sensitive container log text. It must remain on the authenticated admin
interface and should not be anonymously published.

**Cutover:** run the candidate on an alternative port, verify HTTP and
log/container listing, build/push under CI, snapshot the existing compose,
systemd unit and classes mapping, then stop only the old Dozzle container and
the native Python proxy, rebind the new candidate to the original URL/port,
and certify. On failure roll back to the old Compose and systemd proxy
using preserved private config. This does not alter WhatsApp, Zabbix or
the other projects on the AGT.
