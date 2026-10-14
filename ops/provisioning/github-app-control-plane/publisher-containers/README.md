# GitHub distributed CI publishers — OCI candidate

DO NOT START THE PUBLISHER DAEMONS WHILE HOST SYSTEMD PUBLISHERS RUN.
Both services carry the explicit Compose profile activate-after-drain
and are not part of a normal Compose up. This artifact is a reproducible
Docker image / contract; NO production publisher cutover is implied.

## OCI contents
Tracked GitHub App publisher scripts and CI distributed executor scripts
are copied into an image at their expected internal paths. The container
does NOT mount Docker socket, host SSH root directory, or unrestricted
host filesystem. CI scripts connect to workers through SSH and execute
the worker-owned runtime, which may use Docker on the worker machine.

## Missing prerequisites before active deployment
- Establish a dedicated least-privilege SSH identity restricted to CI
  hosts and authorized commands; stage known_hosts, prove remote
  reachability from the Docker bridge, and avoid mounting the AGT root
  SSH identity. CIDR/NAT and CI DNS must be declared in the deployment
  manifest. No StrictHostKeyChecking bypass.
- Stage GitHub App config/installation and private-key credentials as
  private files readable only by the nonroot runtime UID, not from
  the host /etc directory by accident. The runtime must never log the key.
- Stage host registry SQLite and private profile/cache/log directories
  in an explicit persistent backup/restore contract. Prove all expected
  worker registry fields, dependencies, health updates and successful
  execution of python/docker/transport/postgres checks by exact PR SHA.
- Two publishers share the webhook metadata SQLite on a local filesystem.
  Coordinate **all three** services as one writer group: stop receiving
  new deliveries, drain or explicitly checkpoint running jobs, take a
  consistent sqlite3 backup, change mounts atomically, then enable only
  one owner per daemon role. Test replay, deduplication and rollback.
- Fail closed on missing private mounts. A read-only Docker bind of an
  SSH private key or GitHub App key still exposes it to the container:
  this trust boundary requires dedicated credentials and access policy.
- Never merge/declare #324 complete until 11/11 CI required checks
  green, live GitHub API/check roundtrip verified and rollback tested.
- The host-native CI workers are independent and continue functioning
  while the controller is evaluated. This phase never restarts them.

The Dockerfile can be smoke-tested by running its default command in
a network-none, mount-free candidate; only the explicit Compose profile
activates the publisher daemon commands.
