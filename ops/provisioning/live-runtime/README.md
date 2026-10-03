# Live runtime reboot invariants

This package closes the configuration-drift class documented by AFO-2026-004.

The live worker previously depended on an implicit stack of historical Compose
overlays. During a topology rebuild, the effective value
`AGENT_EXECUTION_ENABLED=true` was lost and the worker came back healthy while
all autonomous WhatsApp responses stopped at `AGENT_EXECUTION_DISABLED`.

The files here make the minimum production semantics explicit without
versioning host-private network coordinates.


## Invariants

At reconciliation time the runtime must satisfy:

- database container restart policy: `unless-stopped`;
- worker restart policy: `unless-stopped`;
- worker effective environment: `AGENT_EXECUTION_ENABLED=true`;
- database and worker become healthy before reconciliation succeeds.

`worker-production-execution.compose.yaml` is deliberately small and must be
the final worker Compose overlay. Physical NICs, VLAN addresses, secrets and
other host-specific coordinates remain outside Git.


## Boot behavior

Provide the host-private `/etc/default/attention-router-live-runtime`, then run
`install.sh`. The installer places `reconcile.py` at
`/usr/local/sbin/attention-router-live-runtime-reconcile`, installs the final
worker overlay and systemd unit, enables the unit and performs a read-only
post-install verification.

On every boot the unit:

1. forces the database restart policy to `unless-stopped` and starts it;
2. waits for database health;
3. materializes the worker from the declared local Compose chain;
4. waits for worker health;
5. fails loudly if the execution gate or restart policies drift.

The unit never uses `--remove-orphans` and does not touch Browser/WhatsApp
pairing state.


## Verification

Run the read-only verification after deployment:

```bash
/usr/local/sbin/attention-router-live-runtime-reconcile --check
```

A controlled reboot is the final proof. After reboot, verify the unit is
successful, the database and worker are healthy, and one physical WhatsApp
message completes `INBOUND -> INGRESS -> DECISION -> EXECUTION -> OUTBOX ->
OUTBOUND`.

Do not mark AFO-2026-004 closed before that reboot/E2E proof.
