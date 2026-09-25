# Operational provisioning

This directory holds host-level operational packages that are deliberately kept outside the application runtime.

## Packages

- [GitHub App control plane](github-app-control-plane/README.md): provisions the AGT webhook ingress used by the `andy-github-control-plane` GitHub App.
- [AGT remote operator access](agt-remote-access/README.md): documents the isolated Remote Desktop Commander bridge used for authorized terminal/filesystem access.
- [Heterogeneous distributed CI lab](distributed-ci-lab/README.md): exact-SHA preflight scheduler with duration-aware sharding across bare-metal and KVM workers.
- [Andy Ops Live Supervisor](andy-ops-panel/README.md): LAN-only read-only panel for live compute/CI and Chat-to-console execution visibility.
- [Native OpenTelemetry edge receiver](native-otel-edge/README.md): host-level allowlisted OTLP/HTTP edge and loopback-only Collector publication used for controlled native tracing rollout.

These packages are not tenant contracts and do not expand application authority.

Secrets remain host-local. Never commit:

- GitHub App private keys;
- webhook secrets;
- installation tokens or JWTs;
- PATs or provider credentials;
- Remote Desktop Commander authorization material;
- private runtime network inventories.

The repository contains reproducible code, units, examples and validation steps only.
