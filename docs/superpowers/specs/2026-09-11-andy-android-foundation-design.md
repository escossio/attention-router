# Andy Android Foundation — Architectural Design

Status: **approved design, not implemented**  
Date: 2026-09-11  
Repository authority: `escossio/attention-router`  
Design branch: `docs/andy-android-foundation-design-20260911`

This document captures the architectural decisions approved for the first mobile client of the Attention Router platform. It is a design artifact only. It does **not** claim that the Android application, the Client API, a Kotlin network SDK, human login, device enrollment, tenant bootstrap, Google login, email verification, mobile sync, WhatsApp mobile pairing, or mobile capabilities are already shipped.

The implementation baseline remains the current `main`. No runtime, database, provider account, production transport, live WhatsApp session, migration, container, or deployment is changed by this design.

## 1. Purpose and invariants

The Andy mobile application is intended to become a long-lived product base, not a disposable prototype. The first Android release may be small, but its boundaries must support later addition of location, WhatsApp, Google, Home Assistant, notifications, voice, sensors, biometrics, more tenants, and an iOS client without rebuilding the platform around the first device.

The permanent architectural chain is:

`Human Identity -> Tenant Membership -> Active Tenant -> Device -> Session -> SDK/API -> Capabilities / Channels / Integrations`

The following invariants are frozen by this design:

- Attention Router remains the authoritative backend and policy/execution boundary.
- The mobile client never accesses PostgreSQL, workers, containers, internal ingress, or provider internals directly.
- Android is the first mobile client, not the only possible client.
- A user may belong to multiple tenants, but one tenant is active per application session.
- WhatsApp pairing proves control of a channel installation only. It never creates a tenant, authenticates a human, or grants owner/admin authority.
- Android permissions, device capabilities, tenant authorization, and execution authority are distinct concepts.
- Sensitive execution authority remains server-side and fails closed.
- UI layout is intentionally **not** frozen by this document. Logical product surfaces are frozen; tabs, menu placement, screen names, and visual structure may change later.
- Infrastructure such as AGT01, a particular VPS, local IP addresses, or physical hosts must never become part of the client contract.

## 2. Repository and product boundaries

The recommended repository split is:

- `attention-router`: platform backend, domain model, canonical contracts, API definitions, SDK generation/conformance, server-side identity/tenant/device/session logic, provider-neutral capability/channel/integration boundaries.
- `andy-android`: Android application product, Kotlin UI and Android-specific capability providers. This repository is to be created only when implementation begins.
- Future `andy-ios`: native iOS product using the same platform contracts and semantics.

The existing Python and TypeScript integration SDKs remain part of the platform. They are currently contract-validation SDKs rather than network clients. The Android work should extend the same contract-first discipline, not overload the existing integration wire schema with unrelated human-client concerns.

A future extraction of SDK packages into a dedicated SDK repository is allowed, but must be a packaging/governance change rather than a semantic rewrite. Contract ownership stays explicit.

## 3. Android implementation shape

The Android product is native Kotlin. Jetpack Compose is the current UI direction, but UI technology is not a platform contract and may evolve behind the app boundary.

The application should be modular by responsibility without excessive Gradle fragmentation. The conceptual modules are:

- **App shell**: lifecycle, navigation, theme, global app state.
- **Core**: identity references, active tenant, device state, session state, security primitives, shared models.
- **SDK/API client**: contracts, HTTPS transport, authentication/session exchange, retries, idempotency, structured errors, compatibility negotiation.
- **Features**: onboarding, conversation, console, approvals, profile/tenant management.
- **Capabilities**: location, notifications, camera/QR, microphone, sensors, and later Android-native functions.
- **Integrations**: provider-specific installation UX such as WhatsApp pairing, Google OAuth, Home Assistant setup.
- **Local data/sync**: encrypted cache, outbox, migrations, sync engine.

The goal is isolation: each unit has a clear interface and may change internally without forcing consumers to understand its implementation.

## 4. Human identity, new-device verification, and tenant bootstrap

### 4.1 Human sign-in

The primary human login method is Google. The Android client obtains a Google authentication result and sends the relevant token/assertion to the backend. The backend validates issuer, audience, signature, expiration, nonce/state where applicable, and other required claims. A successful result from Google on the device is not by itself authoritative inside Attention Router.

### 4.2 Email verification for new devices

A new device requires an additional one-time verification sent to the validated Google account email address. This email challenge is required when authorizing a new device, not on every normal application opening.

The server-side challenge must be short-lived, single-use, rate-limited, attempt-limited, bound to the specific authentication transaction and device enrollment attempt, and never logged in plaintext.

The intended sequence is:

`Google validated by backend -> email challenge -> email challenge confirmed -> human identity established/recovered -> device may be enrolled`

### 4.3 Personal tenant creation

For a completely new human identity with no tenant, the backend automatically creates one personal tenant after human identity and new-device verification are complete. That user becomes `OWNER` of the personal tenant.

Creation must be idempotent: retries cannot create multiple personal tenants for one identity.

The same human may later become a member of additional tenants. The personal tenant is not a permanent one-user-only limitation.

### 4.4 One active tenant at a time

The app maintains one active tenant per session. Switching tenants requires the backend to verify membership and issue/refresh session authority for the selected tenant.

The client may assert a desired tenant, but a `tenant_id` supplied by the device never grants authority by itself.

## 5. Device identity and session security

### 5.1 Device keypair

On first installation/enrollment, the app creates a device cryptographic keypair using Android Keystore. The private key is non-exportable from the application and never sent to the backend. Hardware-backed protection should be used when available, but the protocol must not depend on a specific hardware feature being present.

The backend stores the public key and associates it with an enrolled `device_id`.

### 5.2 Device roles

The Android application is enrolled with both existing conceptual roles:

- `CLIENT`
- `CAPABILITY_NODE`

`CLIENT` means the application is a human-facing client of the platform. `CAPABILITY_NODE` means it can expose controlled Android-native capabilities such as location or camera.

### 5.3 Session model

The Android application must not reuse a long-lived integration Bearer credential as its primary login mechanism.

Application sessions are short-lived and bound to:

`human identity + device + active tenant`

Session renewal should prove continued possession of the enrolled device key and revalidate server-side user/device/tenant state. Session material stored locally must be protected with Keystore-backed encryption and must be revocable.

Device revocation on the backend must invalidate future protected operations from that device. A stolen valid session must not become permanent authority.

### 5.4 Normal reopen

After successful first enrollment, normal application startup should be:

`open app -> recover local device identity -> prove/refresh session -> sync active tenant -> app ready`

Google and email verification return only when reauthentication or a new-device enrollment is actually required.

## 6. Bootstrap state machine

The first complete bootstrap is:

1. Create local installation identity.
2. Create Android Keystore keypair.
3. Sign in with Google.
4. Backend validates Google identity.
5. For a new device, backend sends email challenge.
6. User confirms the email challenge.
7. Backend creates or recovers the human identity.
8. Backend creates/retrieves the personal tenant and memberships.
9. Backend enrolls the device public key and roles.
10. Backend issues a short session for the active tenant.
11. SDK requests a bootstrap snapshot.
12. UI reaches the logical state **Andy connected**.

The bootstrap snapshot should return the minimum state required to start the app coherently: authenticated user reference, active tenant, device state, capabilities summary, installed channel/integration summary, pending approvals/attention items, compatibility state, and sync cursor/version where applicable.

## 7. Client API and SDK boundary

### 7.1 Separate Client API from Integration API

Two authority surfaces are required:

- **Client API**: human authentication, memberships, tenant activation, device enrollment, sessions, bootstrap, mobile capabilities, approvals, and user-facing control.
- **Integration API**: provider/channel/capability connector traffic using integration bindings and integration credentials.

The existing neutral integration transport design remains the provider/connector boundary. It must not be repurposed as the Android human-session API.

### 7.2 Domain-oriented API

The API is domain-oriented, not platform-branded around Android. Conceptual resource families include:

- `/api/v1/auth/...`
- `/api/v1/tenants/...`
- `/api/v1/devices/...`
- `/api/v1/sessions/...`
- `/api/v1/capabilities/...`
- `/api/v1/channels/...`
- `/api/v1/integrations/...`
- `/api/v1/approvals/...`
- `/api/v1/bootstrap`

Exact endpoint paths are intentionally not frozen here; the domain boundaries and authority semantics are frozen. The concrete HTTP contract must be versioned and language-neutral before implementation.

### 7.3 Dedicated client contract family

The existing `contracts/integration/v1` contract continues to describe integration/provider messages. Human-client/session/device contracts require a dedicated client contract family rather than adding unrelated login/session semantics to the integration schema.

The final contract artifact format may be OpenAPI, JSON Schema plus a transport profile, or an equivalent language-neutral representation. The implementation plan must choose one format and make it canonical. Generated types may be internal implementation aids; semantic authority remains the versioned contract.

### 7.4 Kotlin SDK responsibility

The Kotlin SDK is the official application boundary to Attention Router. It owns:

- contract types and validation;
- HTTP transport;
- session attachment/renewal;
- idempotency behavior;
- timeout/retry classification;
- structured platform errors;
- compatibility/version negotiation;
- bootstrap calls;
- high-level domain APIs.

The UI must not manually construct URLs, authentication headers, raw JSON contracts, or retry rules.

The SDK does **not** decide Andy policy, authorize external actions, interpret business meaning, or access internal database/runtime components.

A minimal first SDK slice is:

`Auth + Session + Tenant + Device + Bootstrap`

## 8. Local persistence, offline behavior, and sync

### 8.1 Backend remains authoritative

The Android app is an offline-tolerant client, not a second independent engine. Local data exists for performance, availability, and safe offline collection; authoritative identity, tenant membership, policy, approval, and sensitive execution remain server-side.

### 8.2 Local data categories

Local persistence is separated into:

1. **Device secrets**: private key remains in Keystore; sensitive session material is protected by Keystore-backed encryption.
2. **Encrypted state cache**: active tenant, profile, device/capability state, recent conversation data, integration/channel summaries, pending items, compatibility state.
3. **Durable offline outbox**: only data that is valid to originate offline, with tenant, device, timestamps, idempotency key, and payload identity.
4. **Non-sensitive preferences**: visual/theme/UI preferences and similar local-only settings.

Sensitive local storage must be protected from normal Android backup/restore paths unless a future restore protocol explicitly handles cryptographic re-enrollment.

### 8.3 Offline outbox

Location samples, device telemetry, and similar pre-authorized capability output may enter the durable outbox.

Sensitive commands and approvals must not be silently converted into deferred local execution. The app may preserve a draft or user intent, but authority must be revalidated with the backend before external effect.

Retries of an unknown network outcome reuse the same idempotency identity where the server contract declares the operation retry-safe.

### 8.4 Sync engine

The SDK/app has a small sync engine with:

- **push** of eligible outbox items;
- **pull** of server-side changes using a cursor/version mechanism;
- freshness metadata for cached state;
- tenant-scoped local records;
- explicit states such as synchronized, pending, stale, failed, or conflicted.

The UI must never display stale data as if it were current. For example, an old known WhatsApp state is shown as "last known" with its age.

### 8.5 Local schema migrations

The local database is versioned. Application updates require tested forward migrations so that an update cannot silently destroy offline queue state, tenant context, or security metadata.

## 9. Mobile capabilities

### 9.1 Three independent gates

A capability is operational only when all relevant conditions are true:

1. the device/software supports it;
2. Android permission/state permits it;
3. Attention Router tenant/policy authorization permits it.

Android permission alone never grants Andy authority.

### 9.2 Device capability providers

Android-native functions are behind provider interfaces such as:

- Location Provider
- Camera/QR Provider
- Notification Provider
- Microphone Provider
- Sensor Provider

Core app logic asks for a semantic capability and does not depend on provider-specific Android implementation details.

### 9.3 Capability announcement

The device announces its supported/current capabilities with a timestamp. The backend may distinguish declared, provisioned, operational, suspended, revoked, unauthorized, or temporarily unavailable states.

Initial mobile capability candidates are:

- `device.location.current`
- `device.location.background`
- `device.notifications.receive`
- `device.camera.qr`

Exact canonical names must conform to the platform capability registry before implementation.

### 9.4 Location policy

Location supports three user/policy modes:

- disabled;
- on-demand;
- background authorized.

Background collection is never implied by permission to obtain current location.

A location sample must carry enough provenance to distinguish fresh from stale data, including at least sample time, accuracy, provider/source, device identity, tenant, and backend receive time.

Retention is policy-driven; the client must not accumulate indefinite location history by default.

### 9.5 Capability invocation safety

The backend may invoke only typed, registered capabilities. There is no generic remote "execute command" or arbitrary-code capability.

Capabilities may declare interaction requirements such as foreground required, explicit user confirmation required, or biometric confirmation required.

## 10. Channels and integrations

### 10.1 Distinct concepts

- **Device capability**: native function offered by the phone.
- **Channel**: communication medium used by Andy, such as WhatsApp or email.
- **Integration**: external system offering data/actions, such as Google Calendar or Home Assistant.

### 10.2 WhatsApp as Channel Installation

WhatsApp belongs to a tenant as a `Channel Installation`, not as the identity of the app or tenant.

The mobile design supports two installation methods behind one channel lifecycle:

- same-device pairing/handoff;
- traditional QR pairing using another display/device.

Both must terminate in the same backend channel installation identity. The exact WhatsApp/provider mechanics are provider-specific and must be validated during implementation; they must not leak into tenant, device, or core session semantics.

Connecting WhatsApp is optional. A user can complete onboarding and use Andy without a WhatsApp installation.

Existing pilot pairing code is historical proof/context, not automatically the mobile onboarding authority model.

### 10.3 Google identity is not Google integration

Google sign-in proves human identity for application login. Google Calendar, Gmail, Drive, or other provider access requires a separate consent/OAuth installation with provider-specific scopes.

Logging into Andy with Google never silently grants provider data access.

### 10.4 Home Assistant

Home Assistant is an integration provider. The app initiates/provisions the integration, while provider adapters expose semantic capabilities such as climate or lighting operations. The core never depends on a specific Home Assistant URL, local IP, or host placement.

### 10.5 Provider credentials

Persistent provider credentials should be held by the backend/integration secret boundary, not by the Android application. The mobile app may participate in interactive authorization, but should receive installation state rather than retaining long-lived provider refresh credentials.

### 10.6 Installation lifecycle

A generic lifecycle should support states similar to:

`not configured -> authorizing -> connected -> degraded / reauth required -> suspended -> revoked`

Provider-specific installation handlers implement exceptional UX while common installation state remains generic.

## 11. Product surfaces and UX flexibility

The product has four logical surfaces:

- conversation with Andy;
- operational console/status;
- approvals/attention queue;
- administration of account, tenant, devices, capabilities, channels, integrations, and security.

This document deliberately does **not** freeze tab count, navigation hierarchy, screen names, icons, menu position, or final visual design.

Conversation and operational control are both first-class. The app must not become merely a configuration tool or merely a chat shell.

Push notifications are attention triggers only. A sensitive approval is reopened in the app, revalidated against current backend state, and may require biometric confirmation. A lock-screen notification action never carries enough authority by itself to perform a sensitive effect.

## 12. Compatibility and evolution

### 12.1 Versioned contracts

Public client contracts and APIs are versioned. Within a supported major contract version, additive evolution is preferred. Breaking semantics require a new version rather than silent behavior change.

### 12.2 Capability discovery and feature flags

The app reports its application version, SDK version, and supported contract/capability versions. The backend reports available capabilities/features and compatibility requirements.

New features may be enabled by tenant/device/version without forcing the entire app into a new architecture. Unsupported functions stay hidden or explicitly unavailable.

### 12.3 Rollout safety

Play Store rollout should support staged release and server-side feature disablement. A faulty feature can be disabled without making the entire app unusable when the protocol permits it.

Forced update is reserved for situations such as a critical security issue or an unsupported protocol boundary.

## 13. Observability and diagnosis

The app must be diagnosable without exposing secrets.

Required design capabilities are:

- structured local logs with redaction;
- request/correlation IDs end-to-end;
- app version, SDK version, contract version, sync state, device state, and capability state;
- optional/consented crash and technical telemetry;
- a user-triggered sanitized diagnostic bundle.

Logs and diagnostic payloads must not include raw access/refresh tokens, private keys, email verification codes, provider secrets, raw message contents, or raw precise location by default.

A diagnostic bundle may include safe device/app identifiers, versions, capability state, sync cursors/status, integration/channel states, and recent sanitized error codes.

## 14. Security and privacy model

The mobile design extends the repository's existing threat-model principles: the model is not an authorization boundary, policy remains separate, human approval remains separate for critical actions, and external providers remain outside the trusted core.

### 14.1 Zero trust in client claims

The backend validates all client claims. A user-controlled `tenant_id`, `device_id`, capability claim, push payload, cached state, or UI action never grants authority by itself.

### 14.2 Fail closed

If identity, device state, membership, session, capability authorization, or execution authorization cannot be established, the protected operation does not execute.

### 14.3 Mobile-specific threats

The design must cover at least:

- lost or stolen phone;
- extracted local storage;
- stolen or replayed session material;
- modified/tampered client;
- malicious local app/intent/deep-link input;
- push notification spoofing or stale push payloads;
- network interception and TLS failure;
- Google/email challenge replay or brute force;
- cross-tenant cache leakage;
- capability permission confusion;
- provider credential leakage;
- dependency/build compromise;
- downgrade/incompatible-client operation.

### 14.4 Controls

Core controls include:

- Android Keystore non-exportable device private key;
- TLS hostname/certificate verification;
- short-lived revocable sessions;
- server-side membership/device checks;
- single-use rate-limited email challenge;
- tenant-scoped cache and outbox;
- encrypted sensitive local state;
- minimized push payloads;
- revalidation before approvals/sensitive effects;
- provider secrets kept server-side;
- CI/dependency scanning and signed production distribution;
- redacted logs and diagnostics.

Device attestation/root state may later be used as a risk signal, but must not become the sole proof of identity or tenant authority.

## 15. Error and retry semantics

The SDK normalizes transport and platform errors into stable categories such as:

- unauthenticated / reauthentication required;
- new-device verification required;
- tenant forbidden;
- device revoked;
- capability unavailable / permission missing / not authorized;
- rate limited;
- retryable server/network failure;
- contract incompatibility;
- permanent request failure;
- unknown outcome.

Unknown network outcomes do not justify assuming success. Retry-safe operations reuse the same idempotency identity/body where required. Sensitive operations must be re-read/revalidated rather than blindly retried when an external effect might already have occurred.

Error responses and logs never echo secrets or raw sensitive payloads.

## 16. Testing strategy

Implementation must preserve the repository's existing synthetic/offline discipline unless a provider/live test is explicitly authorized.

Required test layers include:

- language-neutral client contract conformance;
- Kotlin SDK unit tests and serialization/error tests;
- Google token validation negative cases on the backend;
- new-device email challenge expiry/replay/rate-limit tests;
- personal-tenant idempotent creation tests;
- tenant membership and tenant-switch isolation tests;
- device enrollment and key-possession tests;
- remote device revocation tests;
- session expiry/renewal/revocation tests;
- bootstrap snapshot tests;
- Android local-store migration tests;
- encrypted-cache/outbox recovery tests;
- offline retry/idempotency tests;
- capability permission/authorization matrix tests;
- stale-state UI/state-model tests;
- push-trigger approval revalidation tests;
- log/diagnostic redaction tests;
- compatibility/version negotiation tests.

Adversarial tests must explicitly attempt cross-tenant access, stale session use, forged device IDs, capability escalation, duplicate enrollment, replayed email challenge, and retry ambiguity.

## 17. First end-to-end milestone

The first implementation milestone should prove the permanent spine rather than a polished UI.

A successful synthetic/staging flow is:

1. Install a clean Android build.
2. Create device keypair.
3. Sign in with Google.
4. Backend validates Google identity.
5. New-device email challenge is sent and confirmed.
6. Backend creates/retrieves human identity.
7. Personal tenant is created/recovered and membership established.
8. Android device is enrolled as `CLIENT + CAPABILITY_NODE`.
9. Short session for the active tenant is issued.
10. Kotlin SDK retrieves bootstrap state.
11. App reaches logical state **Andy connected**.
12. Closing/reopening does not require email verification while the enrolled device remains valid.
13. Server-side device revocation causes protected operations to fail closed on the next authoritative interaction.

This milestone does **not** require WhatsApp, Home Assistant, background location, polished conversation UX, or a production deployment.

## 18. Explicit non-goals for the foundation milestone

The foundation milestone does not:

- finalize the app's visual navigation;
- implement iOS;
- make WhatsApp mandatory;
- migrate the live runtime;
- replace the existing WhatsApp transport automatically;
- redesign the Andy decision engine/policies;
- grant Google provider scopes through Google login;
- store long-lived provider credentials on the phone;
- create arbitrary remote command execution on Android;
- expose database/internal service interfaces to the app;
- couple the app to AGT01 or any specific server;
- claim the currently proposed Client API or network SDK is already shipped.

## 19. High-level implementation order

The detailed implementation plan will be produced only after this written design is reviewed and approved. The intended dependency order is:

1. dedicated Client API contract and server authority model;
2. minimum backend human identity / tenant / device / session slice;
3. Kotlin contract/client SDK slice;
4. separate `andy-android` repository and app shell;
5. Google + email new-device onboarding;
6. tenant/device/session/bootstrap flow;
7. encrypted local state + sync/outbox;
8. first Android capabilities (location, notifications, camera/QR);
9. channel/integration installation flows;
10. conversation/console/approval UX refinement.

Each phase must preserve existing Attention Router behavior unless its own reviewed plan explicitly changes a platform contract.

## 20. Design acceptance summary

The accepted permanent boundaries are:

`Human Identity -> Tenant -> Device -> Session -> SDK/API -> Capabilities / Channels / Integrations`

The accepted product direction is:

- Android first, iOS-compatible architecture from day one;
- Kotlin native client;
- separate Android product repository;
- contract-first client SDK/API;
- Google login plus email verification for each new device;
- automatic personal tenant for a new user;
- one active tenant per session;
- Keystore device identity plus short server sessions;
- device roles `CLIENT + CAPABILITY_NODE`;
- offline-tolerant encrypted cache/outbox without offline authority for sensitive effects;
- location as a first-class capability with disabled/on-demand/background-authorized modes;
- WhatsApp as an optional channel installation supporting same-device and QR methods behind one backend identity;
- Google/Home Assistant and future providers as integrations rather than core special cases;
- conversation and operational control as first-class product surfaces;
- flexible UI, stable architectural boundaries;
- fail-closed security, tenant isolation, revocation, observability, and versioned evolution.

No implementation work starts from this document until the document itself is reviewed and explicitly approved for planning.
