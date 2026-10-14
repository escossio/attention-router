# GitHub App and distributed CI: coordinated cutover gate

This runbook covers the webhook receiver and both check publishers as **one
SQLite consistency domain**. It is a candidate procedure for issue #324.
The current production owner remains the three native systemd units. A merge
does not activate OCI. Never run the activation steps while any gate is NO-GO.

## Communication and authority

| Initiator | Destination | Protocol | Authority | Required |
| --- | --- | --- | --- | --- |
| GitHub | Cloudflare ingress / receiver | HTTPS webhook | HMAC and repository/installation allowlist | yes |
| Receiver | shared local SQLite | file transaction | exclusive writer coordination | yes |
| Each publisher | shared local SQLite | file transaction | single role owner | yes |
| Each publisher | GitHub API | HTTPS | scoped App key/installation token | yes |
| Each publisher | CI workers | SSH | dedicated key and checked host key | yes |

The OCI services use the existing Docker bridge and host loopback bind. No
separate L2 identity, dedicated L3 identity, dedicated IP, static IP, or
additional Docker network is required. Worker reachability grants no GitHub
publishing authority; App credentials grant that separately. The receiver
must not mount the App key.

## Preconditions: all must be recorded as GO

1. Fix a window and name the operator. Record exact source/image SHA, active
   systemd PIDs, Compose config, Cloudflare route, ports, mount paths, UID/GID,
   GitHub App installation, and worker host-key fingerprints without logging
   secret values. Confirm WhatsApp and CI worker health.
2. Run `queue_audit.py --database <active-events.sqlite3>` read-only. Compare
   cursors with last delivery rowid. Resolve **every** PENDING/RUNNING record
   by PR state, exact SHA, check name/ID and worker/process evidence. A stale
   local row, absent remote check, or in-progress remote check is NO-GO until
   an explicit idempotent disposition is reviewed. Never replay blindly.
3. Prove one durable delivery path throughout the transition. The receiver
   returns 202 only after SQLite commit and 200 for a committed duplicate.
   A live native receiver may continue accepting into the original DB while
   publishers drain. Before stopping that receiver, prove a write-freeze
   mechanism that returns non-2xx for unpersisted deliveries **and** a tested
   durable buffer/replay or verified GitHub redelivery window. If either
   proof is absent, remain NO-GO. Do not switch a route to the pilot DB.
4. Certify UID 10001 access to a *current* isolated snapshot, App signing,
   strict SSH, Registry/profiles/log persistence, and `BEGIN IMMEDIATE`
   followed by rollback on the copy. Never mass-chown production SQLite,
   Registry, or native secrets. No root SSH identity in OCI.
5. On the post-drain source database, use `sqlite3.Connection.backup()` to
   a private timestamped directory on local storage. Run `PRAGMA
   integrity_check`, record table counts/cursors, then restore to a separate
   copy and repeat checks. Preserve mode-0600 copies of systemd units,
   Cloudflare config, Compose parameter file and credentials. Retain prior
   backups. The receiver and both publishers must use the **same** restored
   SQLite after handoff, never three independently diverging copies.
6. Test the exact rollback commands on an isolated clone. `compose up`
   without the `activate-after-drain` profile must start no publisher.
   Confirm that any receiver route switch can be reverted and that delivery
   IDs remain deduplicated after replay.

## Transition order after the gate is GO

1. Keep native receiver accepting and persisting while native publishers
   drain. Stop *only* the native publishers after zero real in-flight jobs
   and checkpoint both cursors. Confirm both PIDs exited. No OCI publisher
   may run yet.
2. Preserve delivery ingress during receiver handoff by the proven buffer
   or non-2xx/replay mechanism. Stop native receiver, confirm port release,
   take the final consistent backup, then transfer the **same current DB**
   under the reviewed UID/mount transaction. No publisher may run during
   this transfer. Start OCI receiver and verify local signed request
   persistence plus duplicate handling. Change the Cloudflare route only
   after local health and route config validation; verify a signed GitHub
   delivery through the public route by delivery ID and exact SQLite row.
3. Activate only the candidate publishers with
   `docker compose --profile activate-after-drain up -d postgres-publisher
   shadow-publisher` using the reviewed private parameter file. Confirm
   both mount the same DB as the OCI receiver, exclusive role ownership and
   cursor continuity before allowing new work. Abort if a native publisher
   restarts.
4. Observe exact-SHA check creation/completion by each publisher, cursors,
   queue states, logs, workers and WhatsApp services. Record timestamps and
   owner PIDs/container IDs at every step. Keep native units installed and
   disabled only after the full equivalence/rollback proof.

## Abort and rollback

On any auth, queue, ingress, mount, health, or check mismatch, stop OCI
publishers first and prove their PIDs gone. Preserve the current SQLite and
delivery IDs with a fresh consistent backup; never restore an older copy over
newly acknowledged deliveries. Restore the native writable ownership from
the reviewed snapshot/permissions procedure, then restart one native
publisher per role. Restore the receiver and Cloudflare route using the
saved config, preserving a non-2xx response for unpersisted deliveries
until the native receiver is accepting again. Reconcile every delivery and
cursor added during the attempted switch before lifting the freeze. Prove
one owner per role after rollback. Do not delete containers, units, state,
credentials or backups as part of rollback.

## Current decision

NO-GO: historical RUNNING rows remain unresolved (including a remote
in-progress check), durable ingress buffering/replay has not been certified,
and the complete two-publisher cycle plus coordinated rollback has not been
proven. This runbook does not authorize a production cutover by itself.
