# V0.3C Client Session Authority Implementation Plan

Issue: #64

This plan follows the Git-first sequence proven by V0.3B.

## Phase A — contract/design slice

1. Freeze the session challenge, completion and authenticated bootstrap HTTP contract.
2. Freeze `csc_`, `csn_` and `cst_` public credential/reference shapes.
3. Add infrastructure-independent tenant selection and session-authority guards.
4. Add negative tests for expiry, revocation, device role, Human Identity, device, membership, tenant and time-context mismatches.
5. Merge only after Public CI and CodeQL are green.

No migration, FastAPI runtime, deployment or Android functional code belongs to Phase A.

## Phase B — backend runtime

1. Add fail-closed settings for session challenge TTL and client session TTL.
2. Add migration after `0040_client_device_bootstrap` for isolated session challenge/session persistence.
3. Persist challenge digest and session-token digest only.
4. Add deterministic repository locking and exactly-once completion.
5. Resolve device by server-derived SPKI fingerprint.
6. Revalidate ACTIVE device, CLIENT role, membership and tenant before issuing session.
7. Add bearer authorizer that revalidates current authority on every protected request.
8. Expose:
   - `POST /api/v1/session/device/challenges`;
   - `POST /api/v1/session/device/challenges/{session_challenge_id}/complete`;
   - `GET /api/v1/client/bootstrap`.
9. Add synthetic, HTTP, migration and PostgreSQL concurrency proofs.

## Phase C — Android governance + implementation

1. Admit a governed Android V0.3C frontier.
2. Reuse `andy_device_identity_v1`.
3. Send public SPKI and optional tenant selection assertion.
4. Sign the session challenge with `SHA256withECDSA`.
5. Keep the returned `cst_` credential in memory only.
6. Use it as Bearer for exactly the authenticated bootstrap request in this frontier.
7. Render the first authenticated logical `Andy connected` state without exposing the raw token.

## Phase D — live proof

1. Repository gates green in both repositories.
2. Controlled backend deployment/migration.
3. Preserve the physical Android installation and signing identity.
4. Prove:
   `enrolled device -> possession -> ACTIVE membership -> short session -> authenticated bootstrap`.
5. Prove raw token absence from PostgreSQL/logs/persisted Android files.
6. Prove expired/revoked/unknown session and revoked device/inactive membership fail closed.
7. Stop before renewal, durable mobile session storage or expanded authority.

## Non-negotiable exclusions

- no Google reauthentication as session authority;
- no email challenge;
- no device re-enrollment;
- no refresh token;
- no renewal;
- no default-tenant fallback;
- no capability/channel/integration authority;
- no integration credential reuse;
- no raw session token persistence.
