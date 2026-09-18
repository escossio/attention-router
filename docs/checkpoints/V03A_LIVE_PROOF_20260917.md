# V0.3A live proof checkpoint — 2026-09-17

## Scope

This checkpoint records the first successful physical Android proof of the Human Auth continuation-grant boundary. It intentionally stops before any bootstrap or tenant/session authority is created.

## Authoritative code

- Attention Router backend implementation: `05d5f7d462bf6d09eb5c765177f96329ac863480`
- Android implementation: `escossio/andy-android@a0f4ce7196b7f9fd40bbbb66ce9fcbc3c9f9b8d4`
- Public Client API base URL: `https://api.escossio.com`

The protected Attention Router main had advanced beyond the deployed backend by test/dependency-only changes; no backend redeploy was required for this proof.

## Public Client API surface

The public TLS endpoint exposes the Human Identity routes needed by this frontier:

- `POST /api/v1/auth/google/challenges`
- `POST /api/v1/auth/google/challenges/{challenge_id}/verify`
- `POST /api/v1/auth/google/challenges/{challenge_id}/verify-and-continue`

The V0.3A route was validated through the public proxy with certificate and hostname verification enabled.

## Live proof

A physical Android device running the V0.3A APK completed the real Google Human Identity flow.

Observed sequence:

1. challenge request returned HTTP 201;
2. the user selected a real Google account through the Android credential UI;
3. `/verify-and-continue` returned HTTP 200;
4. Android reached the visible state `Human identity validated.`;
5. the backend Human Auth transaction finished `VERIFIED`;
6. the Human Identity reference was resolved;
7. the proof created exactly one continuation grant with purpose `DEVICE_BOOTSTRAP`.

## Continuation-grant evidence

At the end of the proof:

- grant state: `ACTIVE`;
- `consumed_at`: unset;
- `revoked_at`: unset;
- backend persistence contained the token digest, not the opaque token;
- no persisted credential matching the `hcg_` continuation-token format was found in the Android app's private files.

The Android implementation keeps the grant in memory at this frontier.

## Explicit non-actions

The proof did **not**:

- consume the continuation grant;
- execute device bootstrap;
- create a tenant;
- create a membership;
- create an enrollment;
- create a session;
- mutate Android source;
- deploy new backend code;
- apply a database migration.

## Resume rule

The next product frontier must start from the proven `ACTIVE` `DEVICE_BOOTSTRAP` continuation grant.

Do not repeat Human Identity design or treat the public HTTPS/Google path as unproven unless new evidence invalidates this checkpoint.

The next frontier must separately define and validate the authority transition from the continuation grant into bootstrap state. It must not be folded back into V0.3A.
