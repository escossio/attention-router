# Client API V0.3C — Client Session Authority

Status: Git-first runtime candidate implemented behind `CLIENT_SESSION_ENABLED=false` by default; repository certification is required before merge or deployment.

V0.3C starts from the completed V0.3B device enrollment boundary.

## Flow

```text
ACTIVE enrolled Android device
  -> fresh P-256 possession challenge
  -> server verifies existing enrolled key
  -> ACTIVE tenant membership resolution
  -> short opaque client session
  -> authenticated bootstrap snapshot
```

## Public contract candidate

- `POST /api/v1/session/device/challenges`
- `POST /api/v1/session/device/challenges/{session_challenge_id}/complete`
- `GET /api/v1/client/bootstrap`

The first two are pre-session possession operations. The bootstrap read requires the `ClientSession` bearer scheme.

## Credential boundary

- challenge id: `csc_`;
- session id: `csn_`;
- raw bearer token: `cst_`;
- no refresh token;
- no renewal endpoint;
- runtime persistence must store only token digest.

## Authority boundary

A public SPKI, requested tenant id or bearer token is never sufficient by itself. Server-side device, Human Identity, membership, tenant and session state remain authoritative and are revalidated fail-closed.

## Idempotent challenge start

New retryable challenges use an opaque versioned id, `csc_r1_<entropy>`. The
suffix encodes 32 random bytes, and the 32-byte challenge is SHA-256 over the
domain `attention-router/client-session-challenge/r1`, a NUL separator and
those random bytes. The stored value remains only SHA-256 of the challenge
bytes. The id and challenge are public and reconstructible rather than
credentials; only the enrolled P-256 private-key signature proves possession.

While such a challenge remains `PENDING` and unexpired, a repeated start for
the same server-resolved device and exact `requested_tenant_id` returns the
same challenge id, bytes and expiry under the existing device-row lock. Before
returning it, the server rederives the bytes and compares their digest with the
persisted digest. An unknown id version, a digest mismatch, multiple unexpired
rows or a different requested-tenant context fails closed with the existing
conflict response. Legacy unrederivable rows conflict until they expire; normal
expiry cleanup then creates a retryable challenge.

This handles a lost `201` response without allowing an unauthenticated retry to
invalidate a valid challenge. Completion remains single-use and still checks
the P-256 signature plus current device, Human Identity, membership and tenant
authority. The HTTP request and response shapes do not change.

## Supersession

Do not revive the older email/device-verification flow from the 2026-09-11 client bootstrap plan. V0.3A/V0.3B are the canonical Human Identity and device-enrollment boundaries.

## Next gate

After this contract/design slice is green and merged, implement isolated PostgreSQL persistence, session possession service, bearer authorization, FastAPI routes and concurrency tests as a separate runtime PR.


## Runtime candidate

The V0.3C backend runtime slice adds:

- migration `0041_client_session_authority`;
- digest-only session credential persistence;
- short-lived session possession challenges;
- exactly-once challenge completion;
- server-side active membership/tenant resolution;
- bearer authorization that re-checks current device/membership/tenant authority;
- authenticated bootstrap snapshot;
- unit, HTTP, migration and PostgreSQL concurrency proofs.

No Android functional change, refresh token, renewal or live deployment is part of this runtime candidate.
