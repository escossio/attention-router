# Client API V0.3B — Device Bootstrap Authority

Status: runtime candidate implemented in Git; PostgreSQL/CI certification is required before merge or deployment.

V0.3B begins only after Human Identity V0.3A has produced a valid short-lived `DEVICE_BOOTSTRAP` continuation grant.

## Flow

```text
Human Identity validated
        |
        v
DEVICE_BOOTSTRAP continuation grant
        |
        v
server challenge bound to Android public key
        |
        v
Android Keystore signature
        |
        v
server verifies device possession
        |
        v
tenant/membership resolution
        |
        v
client device enrollment
        |
        v
continuation grant CONSUMED
```

V0.3B deliberately stops before final client-session issuance.

## Email challenge supersession

The older new-device email challenge is not part of V0.3B.

The current design uses two distinct proofs:

- Human Identity: server-validated Google identity -> opaque `human_identity_id`;
- device possession: ECDSA proof using the existing non-exportable Android Keystore P-256 key.

Email is not canonical Human Identity material in the implemented system. Reintroducing a separate second factor requires a future explicit security decision.

## Authority rules

- Client-supplied tenant identifiers never grant authority.
- A newly authenticated Human Identity is never attached implicitly to the legacy/default tenant.
- A Human Identity with no membership receives one server-created personal tenant and ACTIVE OWNER membership.
- Existing ACTIVE memberships are resolved server-side.
- Multiple memberships do not cause V0.3B to invent an active tenant; session/active-tenant selection is deferred.
- The continuation token is never persisted raw.
- The Android private key never leaves Android Keystore.
- The server derives the public-key fingerprint from SPKI.

## Public operations

V0.3B defines:

- `POST /api/v1/bootstrap/device/challenges`
- `POST /api/v1/bootstrap/device/challenges/{bootstrap_challenge_id}/complete`

Both are pre-session operations.

The successful response contains Human Identity, membership and device authority references only. It contains no access token, refresh token or final session.

Detailed design:

[`docs/superpowers/specs/2026-09-17-v03b-device-bootstrap-authority-design.md`](../../superpowers/specs/2026-09-17-v03b-device-bootstrap-authority-design.md)


## Runtime candidate

The Git-first runtime slice is implemented behind `DEVICE_BOOTSTRAP_ENABLED=false` by default. It includes:

- migration `0040_client_device_bootstrap`;
- isolated client membership, client-device and bootstrap-challenge persistence;
- server-side P-256 SPKI validation and fingerprint derivation;
- ECDSA/SHA-256 device-possession verification;
- atomic membership/device resolution plus continuation-grant consumption;
- bounded FastAPI start/complete routes;
- unit, contract, API, migration and PostgreSQL concurrency proofs.

This status is not a live-runtime claim. AGT deployment remains blocked until repository gates pass and the candidate is merged.
