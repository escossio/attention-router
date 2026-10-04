# Contributor automation instructions

Read README.md and the relevant architecture documentation before changing contracts.
Keep changes small and preserve tenant/contact isolation, delivery proof and fail-closed authority.
Use synthetic fixtures. Never place credentials, real conversations or environment inventories in Git.
Run the documented offline tests for affected components. Provider and transport calls require explicit authorization.
Future design documents are not claims of implemented functionality.

## GitHub-first development control loop
- Follow [docs/operations/github-first-development-control-loop.md](docs/operations/github-first-development-control-loop.md) for agentic investigation and remote-runtime work.
- GitHub defines the expected contract and versioned implementation; runtime/AGT is evidence of the currently observed behavior.
- Do not infer that a feature is absent from a failed runtime behavior or missing conversational context. Verify existing contracts, tests and history first.
- Before a tool burst, state purpose, first source, expected scope/duration and whether the work is read-only or state-changing.
- Keep investigations bounded, checkpoint between logical phases, and freeze secondary issues instead of silently switching frontiers.
- Establish artifact provenance before deep debugging whenever an APK, container or other built artifact is involved.

## Dual-track channel context invariant
- Before changing a channel, connector, scheduler, ingress path, context/memory ingestion path or historical import, read [docs/architecture/channel-context-dual-track-v1.md](docs/architecture/channel-context-dual-track-v1.md).
- Every channel/context change MUST account for both tracks: **live continuity** (new authorized events keep Andy current) and **historical acceleration** (available history/import/export is evaluated as a bounded way to make Andy useful faster).
- Historical acceleration never replaces live continuity. It must not silently reseed or corrupt live cursors, weaken tenant/identity/provenance/authority boundaries, or promote inference into fact. Give historical ingestion its own checkpoint/idempotency semantics when implemented.
- Every relevant PR description MUST state its impact on both tracks. If historical acceleration is not implemented in that increment, state whether it is unavailable, unsafe, too disruptive, or intentionally deferred; do not silently forget it.
- This invariant applies to WhatsApp, Gmail and future channels. A provider-specific limitation may change the implementation, not the requirement to evaluate both tracks.

## Container communication and network placement invariant
- Before creating a new container/service or changing its network placement, read [docs/architecture/container-communication-network-placement-v1.md](docs/architecture/container-communication-network-placement-v1.md).
- A new container does **not** imply a new IP, static address, subnet, VLAN, bridge or network attachment. Addressing is a consequence of the communication graph.
- Every relevant PR MUST declare who initiates connections to whom, protocol/port when applicable, required vs optional flows, application authority, and network/trust zone before choosing placement.
- Attach only the networks required by that graph. Multi-network/dual-homed placement requires explicit justification and must not create unintended transit.
- Every relevant PR MUST record whether L2 identity, L3 identity, a dedicated IP and a static IP are actually required, with reasons. Prefer service identity/DNS when fixed addressing is not architecturally necessary.
- Network reachability and application authority are independent controls: being able to reach a service never grants permission to act.
- Do not version private AGT IP plans, host-private inventory or deployment secrets while satisfying this rule.

## Distributed validation policy
- Treat a host with `andy-ci-distributed` installed as the CI control plane, not as a heavy test worker.
- On the control plane, do not run the full PostgreSQL suite, migration-heavy regression suites, full repository test sweeps, or other long CPU/I/O-heavy validation locally while distributed workers are available.
- Use quick local inspection, lint/diff checks, and small targeted tests for diagnosis. Once the candidate is ready for heavy validation, commit and publish the feature-branch SHA, then run `andy-ci-distributed <40-char-sha> postgres` (and the project equivalent for other distributed heavy gates).
- Do not silently fall back to heavy local execution when distributed workers are unavailable or the candidate SHA is not publishable. Report the blocked heavy gate instead. A local heavy run on the control plane requires explicit user authorization.
- If an accidental heavy local run is detected, stop it and move the workload to the distributed workers rather than leaving worker capacity idle.
