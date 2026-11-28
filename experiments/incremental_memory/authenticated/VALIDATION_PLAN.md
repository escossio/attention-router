# Proposed operational acceptance — NOT executed or activated

Tracking: backend #479/#480/#481; Android #103/#104. G3 NO_GO; G6 pending.

## Certified engine

The governance update in #479 comment 7243660372 confirms all 12 checks for V0
`bd7d3f4008d2bb98a81c7bd5eaa49ccaac507865`. Certification is distinct from executing
that SHA on the existing bench. This M1 change does not update or restart M0.

For a future separately authorized acceptance: materialize that exact commit in
an exclusive validation directory; verify Git SHA and pinned image digests before
execution; reserve separate resources and synthetic persistence; apply the V0
Unix-socket/network-none/resource-limit recipe. Never reuse the live M0 volume or
change its revision history. Record independent-process answer, restart, correction,
history and abstention using the versioned acceptance sequence. Record sanitized
IDs/revisions and resource usage. Compare existing services by noninvasive metadata.
Any creation/exec/restart for this plan still needs operational authorization.

## Authenticated M1

First require green exact-head M1 tests on disposable CI PostgreSQL. The fixture
must issue a real signed ClientSession, authorize its synthetic OWNER scope and
retrieve persisted evidence. Expected question: "Qual é a cor do carro de Roberto?";
expected response: "O carro de Roberto é azul.", with its actual evidence ID/revision.
Also require expired/revoked session, wrong tenant/scope and superseded evidence
refusal, bounded delivery lock, historical preservation and infrastructure errors.
No real device enrollment or endpoint activation is implied by those CI tests.

Future ingress proposal: a dedicated experimental HTTPS origin and TLS termination,
restricted to the authorized test device path, its own runtime credentials and
persistence; no production network memberships, credentials or provider access.
This document contains no private address, certificate/key, endpoint or topology.
No service manifests are activated here. Explicitly enumerate network access and
resource names in the protected operational approval before creating that ingress.

The real device must first be legitimately enrolled in the experimental identity
store using the existing enrollment authority. The test fixture's synthetic
pre-enrollment is not a physical onboarding procedure. M1 currently composes
session issuance/bootstrap, not the full human-auth/device-enrollment route family;
that setup remains a real precondition to device acceptance. Do not mint a bearer
manually or import a productive session to bridge it.

## Android lab artifact and device

Main trusted after #105: `3c3f34da8cd1dd9e7f7efdce398514a35a90dee3`.
Candidate #104 is replayed onto that base; no governance/checker changes.

The currently authorized Android frontier does NOT include a coordinator test path
or CI workflow edits. Request separate governance for the exact path
`app/src/test/java/io/github/escossio/andy/ContextQueryCoordinatorTest.kt` and an
approved lab CI build step. Do not reinterpret a passing debug build as lab proof.
The future worker command is `./gradlew --no-daemon :app:testLabUnitTest :app:assembleLab`.
No build is to run on the control plane as a fallback.

Acceptance of the artifact must verify `io.github.escossio.andy.lab`, visible
"Andy Lab", source SHA, APK SHA-256 and signer identity, plus blank-config
fail-closed behavior. Approved configuration uses separate `labAttentionRouterBaseUrl`,
`labMemoryScopeRef` and, if onboarding needs it, `labGoogleWebClientId`; never normal
production properties or credentials as fallback. These are protected operational
configuration, not values committed to GitHub. Validate the configured artifact
again, since it is distinct from a blank-config CI APK.

Only after separate authorization: install the lab package alongside the unchanged
normal package, use its distinct Keystore/session store, complete legitimate
experimental enrollment, issue ClientSession, ask the synthetic question and record
answer/evidence/correlation on screen. Repeat after a new session, correction,
revocation and tenant switch; a superseded response must not remain visible.
Use text only. Do not unlock G3/G6 or restart FUNC-011.

Rollback: remove only authorized new experimental resources or lab package;
retain sanitized evidence. Productive app, M0 data/history and services are untouched.
