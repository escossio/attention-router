# Lifecycle V3 boot package and OFF compatibility

Status: versioned candidate. No systemd unit, boot default, Compose input,
container, flag or database is changed by this package. All values for a real
installation remain root-private on the host.

## Executable layout

`scripts/operations/build_andy_live_runtime_v3_package.py --destination
<empty-absolute-directory>` stages the V3 module, existing composition helper,
direct-exec launcher and empty package initializers. It refuses an existing
destination, emits `package.sha256.json`, and does not install a service. The
staged launcher has mode 0755; code is 0644, receipt 0400 and directory 0755.
The installer must verify the source commit, each SHA-256 and root ownership
before copying a package into a root-controlled, versioned location. Preserve
the prior executable and default as protected backups. Never point the unit to
an editable worktree.

The existing one-shot unit's `ExecStart` invokes a path directly. The staged
launcher uses the `/usr/bin/python3 -I` shebang; it passes command-line
arguments and exit status to `andy_live_runtime_v3.main()` in the same process,
so systemd signals reach that process. Isolated mode ignores `PYTHONPATH` and
the caller's working directory. Before import, the launcher requires
`ATTENTION_LIVE_PACKAGE_ROOT` and SHA-256 pins for the V3 module and helper,
checks ownership/mode and rejects symlinks and nonempty package initializers.
It writes no bytecode. It emits only stable reason codes on pre-import failure
and never calls the legacy controller. The wrapper itself must be hashed,
backed up and installed at the unit's existing path in a future authorized
window; changing the unit is not required by this candidate.

## Boot configuration contract

[`ops/andy-live-runtime-v3/boot.env.example`](../../ops/andy-live-runtime-v3/boot.env.example)
lists names and ordering only. It is deliberately non-executable and has no
host-private paths, tenant, account or hashes. A private boot default must
contain **exactly six** `ATTENTION_LIVE_WORKER_COMPOSE_FILES` entries in the
order base, otel, otel_e2e, execution, groups, visitor_recovery. Copy the six
paths from the current Worker label only after checking each file's owner,
mode, hash and render. Preserve their bytes, including group and visitor
overlays. The manifest pins all six absolute paths and hashes, canonical
Compose render, immutable image reference/ID, OCI revision, scope and critical
environment hashes. A private default must also pin the package root, both
package file hashes, target manifest path and manifest hash.

For the current pre-First-Contact Worker, use the authentic V2 OFF manifest as
the **target**. The controller validates its existing ten critical environment
hashes, `FIRST_CONTACT_ENABLED` absent/false, empty First Contact scope, image
and full runtime parity. No synthetic First Contact fields are added to the
old Worker. It is not a V3 manifest and must keep its V2 version marker.

For a future ON transition, the **target** is a V3 manifest with all thirteen
critical hashes, `first_contact_mode=on`, `FIRST_CONTACT_ENABLED=true` and
tenant/source-account values equal to separate boot pins; the **previous**
manifest is the saved OFF source. The V3 image and six target inputs must be
versioned and pin-matched before any stop. The reverse transition uses the
saved V2 OFF manifest as target and the running V3 ON manifest as previous;
confirmed presentations and ambiguous external effects remain untouched.
Do not leave a stale previous-manifest pin in the boot default after a
successful directed transition: a later reboot would try to verify a source
that is no longer current and correctly fail closed. Clear that pin only with
an explicit CAS-checked boot update after verification.

## Validation and operational boundary

Use `--check` for an already-running target and `--transition-check` before
an authorized source-to-target change. Neither mode mutates Docker. The
default reconcile mode can update/start the declared DB and recreate the
declared Worker; it is **not** a verification command. Missing private config,
overlay drift, image mismatch, wrong revision, bad health or permissions stop
before a Worker recreate. The current production unit/default and all runtime
services remain unchanged until a separate installation window.

Live continuity remains the existing admitted-event path. Historical
acceleration is intentionally deferred in this lifecycle packaging increment;
there is no cursor reset, history import or replay. The package creates no
container, network or endpoint. It adds no L2/L3 identity, dedicated/static IP
or application authority; systemd invokes the existing local Docker CLI and
reads the same root-private Compose files.
