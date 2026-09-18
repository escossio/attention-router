# V0.3B Device Bootstrap Authority Design

Date: 2026-09-17

Repository authority: `escossio/attention-router`

Status: approved design frontier, implementation may proceed only inside the boundaries below.

## 1. Context

V0.3A is live-proven. A real Android client can:

`Android Device Identity READY -> Google Human Identity -> Attention Router verification -> DEVICE_BOOTSTRAP continuation grant`

At the end of V0.3A the continuation grant is intentionally still `ACTIVE` and unconsumed. No tenant membership, device enrollment, active tenant or final client session exists because of that flow.

V0.3B converts that short-lived continuation authority into durable server-side bootstrap authority.

## 2. Supersession: no email challenge in V0.3B

The older Android foundation design included a single-use email challenge for every new device.

That requirement is superseded for V0.3B.

The implemented Human Identity boundary does not use email as canonical identity material. Human Identity is established from a server-validated Google provider subject and represented by an opaque `human_identity_id`.

The Android client also already owns a non-exportable EC P-256 signing key in Android Keystore.

The V0.3B authority chain therefore is:

`server-validated Human Identity -> short-lived purpose-bound continuation grant -> cryptographic proof of device-key possession -> server-side tenant/membership resolution -> device enrollment`

The device proof is not a second proof of who the human is. It proves that the client controls the device key being enrolled.

Reintroducing an independent email or other second factor later requires a separate explicit security frontier.

## 3. Permanent authority rules

- Attention Router is authoritative for membership, tenant resolution and device enrollment.
- Android never chooses its own authoritative `tenant_id`, `human_identity_id`, `device_id`, role or status.
- A continuation token is sensitive, short-lived, single-use and purpose-bound to `DEVICE_BOOTSTRAP`.
- Raw continuation tokens are never persisted by the backend.
- Android private keys never leave Android Keystore.
- Public-key fingerprints are derived by the server from supplied SPKI; they are not trusted as independent client claims.
- Provider subject/email/name never appear inside canonical tenant or device identifiers.
- Existing legacy/default tenant state must not absorb a newly authenticated human implicitly.
- Failure is fail-closed.

## 4. Device proof protocol

### 4.1 Start

The client submits:

- continuation grant token;
- public key in SPKI DER encoded as unpadded base64url;
- canonical device name;
- platform;
- requested device roles.

V0.3B permits Android platform and `CLIENT` / `CAPABILITY_NODE` roles only.

The server:

1. validates the continuation token by digest lookup without consuming it;
2. resolves the grant's `human_identity_id`;
3. decodes and validates an EC P-256 public key;
4. derives the canonical SHA-256 fingerprint from the SPKI bytes;
5. creates a short-lived bootstrap challenge bound to:
   - continuation grant row;
   - Human Identity;
   - public-key fingerprint;
   - canonical device descriptor;
6. returns only an opaque challenge id, random challenge bytes and expiry.

The challenge bytes are not authority on their own.

### 4.2 Complete

The client signs the exact challenge bytes with the Android Keystore private key and submits the signature.

The server locks the bootstrap challenge and continuation grant, verifies:

- challenge state is `PENDING`;
- challenge is not expired;
- continuation grant is still `ACTIVE`;
- grant is not expired/revoked/consumed;
- purpose is `DEVICE_BOOTSTRAP`;
- Human Identity linkage matches;
- ECDSA/SHA-256 signature is valid against the bound P-256 public key.

Only after those checks may durable bootstrap state change.

## 5. Tenant and membership resolution

V0.3B introduces explicit Human Identity membership authority.

Canonical membership fields:

- opaque membership id;
- `human_identity_id`;
- `tenant_id`;
- role: `OWNER | ADMIN | MEMBER`;
- status: `ACTIVE | SUSPENDED | REVOKED`;
- creation/update timestamps.

Resolution rule:

1. load ACTIVE memberships for the Human Identity;
2. if one or more exist, do not create a tenant;
3. if none exist, atomically create/recover one personal tenant and one ACTIVE OWNER membership;
4. the newly created tenant uses opaque/random identifiers and a non-personal random slug;
5. never fall back to `DEFAULT_TENANT_ID`.

For V0.3B response selection:

- if a new personal tenant was created, it is the initial resolved tenant;
- if exactly one ACTIVE membership exists, that tenant is the initial resolved tenant;
- if more than one ACTIVE membership exists, V0.3B returns the memberships but does not invent an active-tenant choice. Active-tenant selection belongs to V0.3C/session authority.

This avoids making a client-selected tenant authoritative before session issuance exists.

## 6. Device enrollment model

The Android device identity represents the physical/application installation and should not be duplicated merely because the human belongs to multiple tenants.

V0.3B therefore treats the canonical client device as Human-Identity-scoped rather than tenant-scoped.

Canonical fields:

- opaque `device_id`;
- `human_identity_id`;
- public-key fingerprint;
- public-key SPKI needed for future possession verification;
- canonical name;
- platform;
- roles;
- status: `ACTIVE | REVOKED`;
- timestamps.

The legacy `devices` / `device_identities` tables remain separate platform-era structures. V0.3B must not silently reinterpret them as the new client-auth authority.

A repeated successful bootstrap for the same Human Identity and same fingerprint must recover the same ACTIVE device, not create duplicates.

The same fingerprint must not bind to two different Human Identities.

## 7. Atomic successful transition

The successful completion transaction performs, atomically:

1. tenant/membership resolution;
2. device create/recovery;
3. bootstrap challenge -> `VERIFIED`;
4. continuation grant -> `CONSUMED`.

If any step fails, the transaction rolls back and the grant remains usable until its normal expiry unless its security state requires rejection.

The grant must never be consumed before signature verification succeeds.

## 8. Public Client API contract

V0.3B adds exactly two pre-session operations:

- `POST /api/v1/bootstrap/device/challenges`
- `POST /api/v1/bootstrap/device/challenges/{bootstrap_challenge_id}/complete`

No bearer session is required because V0.3B is the bridge before session issuance.

The start request contains the continuation credential. The completion request contains only the device signature; the server-side challenge already binds the continuation authority and device descriptor.

A successful completion returns a bounded authority snapshot:

- status `DEVICE_BOOTSTRAP_ESTABLISHED`;
- `human_identity_id`;
- membership views;
- `initial_tenant_id` nullable when multiple memberships require later selection;
- device view.

It returns no access token, refresh token or final application session.

## 9. Persistence

New V0.3B persistence is isolated from legacy platform device authority:

- `client_tenant_memberships`;
- `client_devices`;
- `device_bootstrap_challenges`.

The existing `human_auth_continuation_grants` table remains the continuation authority source.

Important constraints:

- one membership per `human_identity_id + tenant_id`;
- at most one personal OWNER bootstrap membership is created for a Human Identity under first-bootstrap races;
- unique client device public-key fingerprint;
- bootstrap challenge bound to one continuation grant;
- state constraints are explicit;
- challenge stores a digest of random challenge bytes rather than relying on plaintext as durable authority;
- continuation raw token is never stored.

## 10. Replay and race safety

Required PostgreSQL proofs:

- same continuation token cannot complete twice;
- two concurrent completions cannot create two personal tenants;
- two concurrent completions cannot create duplicate OWNER memberships;
- two concurrent completions cannot create duplicate device rows;
- expired/revoked/consumed continuation grants fail closed;
- wrong private key fails without consuming the grant;
- challenge replay after success fails;
- cross-Human-Identity device fingerprint binding fails.

SQLite/unit tests are not sufficient certification for these concurrency properties.

## 11. Explicit exclusions

V0.3B does not:

- issue final client sessions;
- issue refresh tokens;
- select/switch an active tenant when multiple memberships exist;
- grant capabilities;
- pair WhatsApp;
- create integration credentials;
- authorize external effects;
- introduce email verification;
- redesign legacy platform tenant/device models;
- expose provider identities.

Those belong to later frontiers.

## 12. Exit proof

V0.3B is complete only when a physical Android live proof demonstrates:

`existing V0.3A continuation grant -> device challenge -> Android Keystore signature -> HTTP success -> tenant/membership resolved -> client device enrolled -> continuation grant consumed`

and confirms:

`SESSION_CREATED=NO`

`REFRESH_TOKEN_CREATED=NO`

`CAPABILITY_GRANT_CREATED=NO`
