# Operational provisioning

This directory holds host-level operational packages that are deliberately kept outside the application runtime.

## Packages

- [GitHub App control plane](github-app-control-plane/README.md): provisions the AGT webhook ingress used by the `andy-github-control-plane` GitHub App.
- [AGT remote operator access](agt-remote-access/README.md): documents the isolated Remote Desktop Commander bridge used for authorized terminal/filesystem access.
- [Heterogeneous distributed CI lab](distributed-ci-lab/README.md): exact-SHA preflight scheduler with duration-aware sharding across bare-metal and KVM workers.

These packages are not tenant contracts and do not expand application authority.

Secrets remain host-local. Never commit:

- GitHub App private keys;
- webhook secrets;
- installation tokens or JWTs;
- PATs or provider credentials;
- Remote Desktop Commander authorization material.

The repository contains reproducible code, units, examples and validation steps only.
