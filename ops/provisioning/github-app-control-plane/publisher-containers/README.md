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

## Nonpublishing preflight (added 2026-10-14)

The image includes `/usr/local/lib/andy-github-app/preflight.py`.
Run it as a **one-shot command** in a container using the same explicit mounts and
UID as the publisher Compose. It does not start either daemon, execute a
distributed suite, write a GitHub check, or consume delivery queues.

By default it validates UID/GID, profile/log availability, SQLite integrity,
expected event and job tables, installed worker capacity, and local GitHub
App signing. Optional `--worker ci01 --worker ci02 --worker ci03` verifies
SSH authentication and a remote executable check without dispatching work.
Optional `--github-auth` only obtains an installation token and verifies
`checks:write`; the token is neither stored nor printed.

**Never use `--sqlite-write` against active production databases.** It briefly
tests `BEGIN IMMEDIATE` followed by rollback, and is intended only for
independent validation snapshots or a deliberately drained cutover. Any
preflight failure blocks activation, without printing key material or tokens.

Production `/var/lib/andy-github-app` and `/var/lib/andy-ci` use native
ownership and must **not** be bind-mounted writable into the nonroot OCI
process without a reviewed ownership transition. Bootstrap a private,
UID-10001-owned staging tree with consistent SQLite backups, worker profiles,
logs, App configuration/key, and the dedicated SSH identity. Keep this
outside Git. The original host key must never be mounted into the image.

### Lab certification record (2026-10-14)

- Dedicated SSH identity from Docker bridge authenticated successfully to
  CI01, CI02 and CI03; the original host SSH authentication also passed.
- App key local JWT signing, SQLite transaction/rollback on independent
  snapshots, and App installation-token authorization passed as UID 10001.
- An OCI-controlled transport run against PR #329 SHA `f223d32f` passed on
  CI02 (114 seconds), **without** any candidate publisher daemon active.
- The preliminary snapshots are **not** cutover backups. Always resnapshot
  after an intentional drain; never claim a live clone is current.
- Cutover remains blocked until in-flight native jobs finish, both publisher
  workloads are exercised safely, exact-SHA check publishing is proven
  with a single owner, webhook transition and replay are verified, and
  coordinated rollback is rehearsed. GitHub App webhook receiver and two
  publishers must switch as one consistency domain.

SSH `authorized_keys restrict` disables forwarding and PTY, but does
**not** restrict remote commands by itself. Forced-command constraints
require compatibility tests with the executor and its remote Git/SCP calls.

- An OCI-controlled PostgreSQL run against the same SHA passed on CI03: 463 tests across 57 files, one wave, no degradation, 249 seconds.
- Production SQLite still contains three historical rows in `RUNNING` (PR #206 dated 2026-10-02; PR #303 dated 2026-10-07), without a stored check-run ID. They are **not** evidence of an active process; reconcile their terminal state with GitHub before any queue/ownership transfer. Do not silently replay them or mark them completed.
