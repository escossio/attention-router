# V0.3C Client Session Authority Design

Status: design and contract frontier for issue #64. No runtime persistence, HTTP implementation, deployment or Android functional change is part of this slice.

## 1. Starting authority

V0.3C begins only after V0.3B has already established a canonical Human Identity, ACTIVE tenant membership and ACTIVE enrolled Android device backed by the physical Android Keystore P-256 key.

It does not repeat Google sign-in and does not enroll the device again.

## 2. Authority chain

```text
ACTIVE enrolled device
        |
        v
fresh server session challenge
        |
        v
Android Keystore P-256 signature
        |
        v
server verifies enrolled device possession
        |
        v
ACTIVE membership + tenant resolution
        |
        v
short client session
        |
        v
authenticated bootstrap snapshot
```

## 3. Device lookup

The session-start request presents the existing public SPKI, not a client-selected device id or Human Identity id. The server validates P-256, derives the fingerprint, resolves the enrolled ACTIVE device and obtains Human Identity authority from the server-side device record.

The private key never leaves Android Keystore.

## 4. Active tenant

An optional `requested_tenant_id` is a selection assertion only.

- one ACTIVE membership and no assertion: select that membership;
- multiple ACTIVE memberships and no assertion: fail with active-tenant-required;
- assertion matching one ACTIVE membership: accept;
- unknown/inactive/foreign tenant assertion: deny;
- zero ACTIVE memberships: deny;
- never fall back to `DEFAULT_TENANT_ID`.

Active tenant is scoped to the client session, not a global singleton.

## 5. Session credential

The wire token is frozen as:

`cst_<43 base64url characters>`

The session id is:

`csn_<opaque>`

The token is short-lived, opaque and returned once. Runtime persistence must store only a cryptographic digest. V0.3C has no refresh token and no renewal endpoint.

## 6. Possession challenge

The challenge id is:

`csc_<opaque>`

The challenge is short-lived, single-use, replay-safe and race-safe. Runtime implementation should reuse the V0.3B P-256 verification primitive.

## 7. Session authority

Every authenticated request must re-evaluate current server-side state. An unexpired token is not sufficient when the session is revoked, device is revoked, CLIENT role is absent, membership is inactive, tenant is inactive or any Human Identity/device/tenant binding mismatches.

## 8. Authenticated bootstrap

`GET /api/v1/client/bootstrap` requires the V0.3C ClientSession bearer scheme.

The snapshot contains only:

- contract version;
- Human Identity reference;
- active tenant;
- ACTIVE memberships;
- current device;
- session expiry;
- server time.

Capabilities, channels, integrations, pending work and sync state are deferred.

## 9. Superseded material

The 2026-09-11 bootstrap plan contains useful session invariants but its Google/email/new-device-verification flow is superseded by V0.3A/V0.3B and must not return.

## 10. Stop boundary

This slice and V0.3C as a whole stop before:

- refresh token or renewal;
- persistent Android session storage;
- active-tenant switching inside a session;
- capability/channel/integration authority;
- provider identity disclosure;
- generic remote commands.
