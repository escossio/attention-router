# GitHub App webhook — candidate OCI receiver

This is an isolated receiver, not a replacement for the GitHub CI
publishers. It packages the Git-tracked multi-repository HMAC-verifying
receiver and retains original SQLite stage/delivery tables. No GitHub private
key or GitHub installation-token capability belongs in this container.
The only secret is a dedicated webhook HMAC secret, provided at runtime,
never committed.

## Safety gates

- Pilot must bind only to loopback on an alternative HTTP port and use an
  isolated SQLite directory plus a dummy HMAC secret. Never attach the
  public Cloudflare route to a test candidate, and never mount the live DB
  into a simultaneously running second writer.
- Verify 200 health, unsigned/invalid HMAC 401, valid HMAC accepted 202,
  replay duplicate 200, wrong repository 403, wrong installation 403,
  bounded request size. Test real queue and continuation schemas locally.
- 10001:10001 non-root, read-only rootfs, cap-drop ALL, private state
  and secret mounts. Bind secret readable only by the configured service
  UID and its administrator. Do not mount the App private key in receiver.
- Cutover NOT ALLOWED until the shared SQLite ownership, WAL/journal,
  writer coordination and independent distributed check publishers are
  certified together (issue #324). The running native publishers currently
  invoke host-level /usr/local/lib/andy-ci/bin executors. Moving only
  the receiver can break checks and duplicate publications.
- Before prod: refresh consistent online SQLite backup with Python
  sqlite3.Connection.backup() into private restricted backup
  storage, snapshot native units/config without printing secrets, ensure
  checks quiet or drain safely, version routing, rollback and remote health.
- Production must pass a signed delivery through real Cloudflare ingress
  and the existing distributed-postgres/python/transport/docker checks.
  This pilot does not prove public ingress or publisher containerization.

The production deployment profile must supply its own private paths,
approved repository list and exact installation ID. No host IP or VLAN is
hardcoded in the image. Image build must be done with pinned base digest
before signed release. All paths and bind ports are deployment inputs.
