# Client API V0.3C — Client Session Authority

Status: contract + pure authority model candidate. Runtime implementation is not included.

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

## Supersession

Do not revive the older email/device-verification flow from the 2026-09-11 client bootstrap plan. V0.3A/V0.3B are the canonical Human Identity and device-enrollment boundaries.

## Next gate

After this contract/design slice is green and merged, implement isolated PostgreSQL persistence, session possession service, bearer authorization, FastAPI routes and concurrency tests as a separate runtime PR.
