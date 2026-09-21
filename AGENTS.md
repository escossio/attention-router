# Contributor automation instructions

Read README.md and the relevant architecture documentation before changing contracts.
Keep changes small and preserve tenant/contact isolation, delivery proof and fail-closed authority.
Use synthetic fixtures. Never place credentials, real conversations or environment inventories in Git.
Run the documented offline tests for affected components. Provider and transport calls require explicit authorization.
Future design documents are not claims of implemented functionality.

## Distributed validation policy
- Treat a host with `andy-ci-distributed` installed as the CI control plane, not as a heavy test worker.
- On the control plane, do not run the full PostgreSQL suite, migration-heavy regression suites, full repository test sweeps, or other long CPU/I/O-heavy validation locally while distributed workers are available.
- Use quick local inspection, lint/diff checks, and small targeted tests for diagnosis. Once the candidate is ready for heavy validation, commit and publish the feature-branch SHA, then run `andy-ci-distributed <40-char-sha> postgres` (and the project equivalent for other distributed heavy gates).
- Do not silently fall back to heavy local execution when distributed workers are unavailable or the candidate SHA is not publishable. Report the blocked heavy gate instead. A local heavy run on the control plane requires explicit user authorization.
- If an accidental heavy local run is detected, stop it and move the workload to the distributed workers rather than leaving worker capacity idle.
