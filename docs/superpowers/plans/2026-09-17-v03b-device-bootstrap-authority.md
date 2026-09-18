# V0.3B Device Bootstrap Authority Implementation Plan

Date: 2026-09-17

Design authority: `docs/superpowers/specs/2026-09-17-v03b-device-bootstrap-authority-design.md`

Issue: #60

Implementation branch: `feat/v03b-device-bootstrap-authority`

## Objective

Implement the next authority transition after the live-proven V0.3A flow:

`DEVICE_BOOTSTRAP grant -> cryptographic device proof -> tenant/membership resolution -> device enrollment -> grant consumption`

Stop before final client-session issuance.

## Non-negotiable decisions

- No email challenge in V0.3B.
- No client-supplied tenant authority.
- No reuse of the legacy default tenant for a newly authenticated Human Identity.
- No reuse/reinterpretation of legacy `DeviceRow` / `DeviceIdentityRow` as client-auth authority.
- No raw continuation token persistence.
- No final client access/refresh token.
- Android private key remains non-exportable.

---

## Task 1 — Contract and pure authority vocabulary

### Files

- modify `contracts/client/v1/client-api.openapi.json`
- create `attention_router/core/client/__init__.py`
- create `attention_router/core/client/bootstrap.py`
- create `tests/client/test_device_bootstrap_contract.py`
- create `tests/client/test_device_bootstrap_core.py`

### Contract operations

Add exactly:

- `POST /api/v1/bootstrap/device/challenges`
- `POST /api/v1/bootstrap/device/challenges/{bootstrap_challenge_id}/complete`

### Start request

`DeviceBootstrapChallengeRequest`

Required:

- `continuation_token`
- `public_key_spki_b64url`
- `canonical_device_name`
- `platform`
- `roles`

Forbidden:

- `human_identity_id`
- `tenant_id`
- `device_id`
- `membership`
- provider subject/email/name
- access/refresh tokens

### Start response

`DeviceBootstrapChallengeResponse`

- `bootstrap_challenge_id`
- `challenge_b64url`
- `expires_at`

### Complete request

`DeviceBootstrapCompleteRequest`

- `device_signature_b64url`

### Complete response

`DeviceBootstrapEstablishedResponse`

- `status = DEVICE_BOOTSTRAP_ESTABLISHED`
- `human_identity_id`
- `memberships[]`
- nullable `initial_tenant_id`
- `device`

No session fields.

### Core vocabulary

Define immutable enums/dataclasses for:

- `TenantRole`
- `MembershipStatus`
- `ClientDeviceStatus`
- `ClientDevicePlatform`
- `ClientDeviceRole`
- `TenantMembershipView`
- `ClientDeviceView`
- `DeviceBootstrapChallenge`
- `DeviceBootstrapEstablished`

Core code remains infrastructure-independent.

---

## Task 2 — Persistence model and migration

### Files

- create `attention_router/infrastructure/client_bootstrap_models.py`
- create `alembic/versions/0040_client_device_bootstrap_v03b.py`
- create/update PostgreSQL migration tests

### Tables

#### client_tenant_memberships

- id
- human_identity_id FK
- tenant_id FK
- role
- status
- created_at
- updated_at
- unique(human_identity_id, tenant_id)

#### client_devices

- id
- human_identity_id FK
- public_key_fingerprint
- public_key_spki
- canonical_name
- platform
- roles
- status
- created_at
- updated_at
- unique(public_key_fingerprint)

#### device_bootstrap_challenges

- id
- continuation_grant_id FK
- human_identity_id FK
- public_key_fingerprint
- public_key_spki
- canonical_device_name
- platform
- roles
- challenge_digest
- state
- created_at
- expires_at
- verified_at
- unique(continuation_grant_id)

States:

- `PENDING`
- `VERIFIED`
- `REJECTED`

No raw continuation token column.
No private-key material.

---

## Task 3 — Repository operations

### Files

- create `attention_router/infrastructure/client_bootstrap_repository.py`
- tests for transaction/race behavior

Required operations:

- validate/lock continuation grant without consuming it;
- create bootstrap challenge;
- lock bootstrap challenge;
- resolve ACTIVE memberships;
- create/recover personal tenant + OWNER membership;
- create/recover client device;
- mark challenge verified/rejected;
- consume continuation grant only in successful completion transaction.

### First-personal-tenant race invariant

Concurrent first bootstrap for one Human Identity must converge on one personal tenant and one ACTIVE OWNER membership.

Use PostgreSQL uniqueness/locking rather than application-only optimistic assumptions.

---

## Task 4 — Cryptographic device proof

### Files

- create `attention_router/security/device_keys.py`
- focused crypto tests

Requirements:

- decode unpadded base64url SPKI;
- parse DER public key;
- require EC key;
- require curve P-256 / secp256r1;
- canonical fingerprint = `sha256:<lowercase hex digest of SPKI bytes>`;
- verify `SHA256withECDSA` signature over exact server challenge bytes;
- reject malformed key/signature without leaking crypto internals.

Do not trust a client-supplied fingerprint.

---

## Task 5 — Application service

### Files

- create `attention_router/application/client_bootstrap.py`
- focused service tests

### start_device_bootstrap

1. validate continuation token shape;
2. digest and lock/inspect grant;
3. require `ACTIVE`, correct purpose and non-expired;
4. derive Human Identity from the grant;
5. validate device public key and descriptor;
6. generate random challenge;
7. persist challenge digest/binding;
8. return raw challenge bytes only to caller.

Do not consume grant.

### complete_device_bootstrap

1. lock challenge;
2. validate challenge state/expiry;
3. lock linked continuation grant;
4. validate grant still usable;
5. verify ECDSA signature;
6. resolve memberships;
7. create/recover personal tenant when membership set is empty;
8. create/recover client device;
9. mark challenge VERIFIED;
10. consume continuation grant;
11. return bounded bootstrap snapshot;
12. commit is caller-owned / transactionally atomic.

Wrong signature must not consume the continuation grant.

---

## Task 6 — HTTP Client API

### Files

- create `attention_router/api/v1/client_bootstrap.py`
- modify `attention_router/web/app.py`
- create focused API tests

Error vocabulary must be bounded and provider-neutral:

- `DEVICE_BOOTSTRAP_GRANT_REJECTED`
- `DEVICE_BOOTSTRAP_CHALLENGE_NOT_FOUND`
- `DEVICE_BOOTSTRAP_CHALLENGE_EXPIRED`
- `DEVICE_BOOTSTRAP_CHALLENGE_CONSUMED`
- `DEVICE_BOOTSTRAP_DEVICE_KEY_INVALID`
- `DEVICE_BOOTSTRAP_SIGNATURE_INVALID`
- `DEVICE_BOOTSTRAP_MEMBERSHIP_CONFLICT`
- `DEVICE_BOOTSTRAP_DEVICE_CONFLICT`
- `DEVICE_BOOTSTRAP_UNAVAILABLE`

No database or cryptography exception text crosses the HTTP boundary.

---

## Task 7 — Concurrency and PostgreSQL certification

Add integration tests proving:

- one continuation grant cannot complete twice;
- concurrent first bootstrap creates exactly one personal tenant;
- concurrent first bootstrap creates exactly one OWNER membership;
- concurrent same-key enrollment creates exactly one client device;
- same public-key fingerprint cannot bind to another Human Identity;
- wrong signature leaves grant ACTIVE;
- expired/revoked/consumed grant fails;
- expired/replayed challenge fails;
- transaction rollback leaves grant unconsumed.

Run the repository's normal Public CI and PostgreSQL integration gate.

---

## Task 8 — Android governance before Android functional changes

Repository: `escossio/andy-android`

First create/merge a governance-only frontier:

`.github/architecture/frontiers/android-device-bootstrap-v0-3b.json`

It must allow only the exact Android paths needed for:

- exposing the existing public device key through a core abstraction;
- signing a server bootstrap challenge with the existing Keystore key;
- Kotlin Client API bootstrap request/response support;
- onboarding transition after Human Identity validation;
- focused tests.

Do not mix governance-frontier creation and functional Android changes in the same PR.

---

## Task 9 — Android functional slice after frontier approval

Expected paths:

- `core/device-identity/**`
- `data/device-identity/**`
- `sdk/client-api/**`
- `features/onboarding/**`
- `app/**` only if composition wiring is required
- focused tests only

Android must:

1. retain the V0.3A continuation grant only in memory;
2. obtain the existing device public SPKI;
3. call bootstrap challenge start;
4. sign returned challenge with `andy_device_identity_v1`;
5. complete bootstrap;
6. clear continuation grant after successful completion;
7. expose bounded state showing tenant/membership/device references;
8. persist no continuation credential;
9. create no session locally.

---

## Task 10 — Public HTTPS / live proof

After GitHub CI is green and code is merged:

1. deploy backend migration/code to controlled runtime;
2. add public Apache route(s) only if absent;
3. rebuild/install Android with the permanent Andy signing key;
4. execute a real Human Identity -> V0.3A continuation -> V0.3B device bootstrap flow;
5. capture GoAccess/Dozzle and database evidence;
6. verify continuation grant changes ACTIVE -> CONSUMED;
7. verify exactly one membership/device authority result;
8. verify no session/access/refresh token exists.

Stop at:

`V03B_LIVE_PROOF=PASS`

Do not start V0.3C in the same live session.

---

## Git-first operating rule

GitHub is the implementation/control-plane authority.

Whenever work can be prepared and validated in GitHub, do it there first:

- design;
- contract;
- tests;
- implementation;
- CI;
- review checkpoints.

AGT is then used for what GitHub cannot prove:

- runtime/deployment;
- PostgreSQL live migration/application;
- Apache/runtime wiring;
- Android build/sign/install;
- ADB;
- physical-device cryptographic proof;
- live HTTP/database evidence.

This minimizes idle runtime windows and prevents the AGT from becoming the only place where implementation knowledge exists.
