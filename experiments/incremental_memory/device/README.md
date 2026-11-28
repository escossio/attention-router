# M2-LAB — dormant physical-device preparation

Image certification: `certify-image.sh <published-SHA> <output-directory>` on an
authorized CI worker. Public CI has a dedicated M2 job for this experimental branch.
Receipts distinguish the Docker image configuration digest from the archive SHA256;
no registry manifest is claimed without publishing. See [PREFLIGHT.md](PREFLIGHT.md)
for prepared OAuth/TLS/HBA/socket/isolation configuration and remaining conditions.

Issue #479 / Android #103. Base M1 #481 is preserved; no deployment or network
activation is part of this change. Android #106 must be legitimately incorporated
before changing its test frontier. G3 NO_GO; G6 pending.

`composition.create_device_app` reuses all three existing router factories and
HumanIdentityService, DeviceBootstrapService, ClientSessionService plus M1 delivery.
The only provider verifier constructed by the runtime is GoogleIdentityVerifier:
real signature/audience/issuer validation, then an explicit allowlist of Google sub
identifiers and validity times. A mock exists ONLY via monkeypatch in disposable
CI. No configuration option selects a mock. No session is manually minted.

Provision a NEW dedicated M1-shaped database, then explicitly run
`provision_onboarding` with its experimental role names. It creates four existing
onboarding tables, writer fences and narrowly identity-only issuer grants. The
query role stays read-only. Never run this against M0, a previous acceptance DB
or production. No cognitive migrations, global create_all or schema work at startup.

The real sequence is Google nonce challenge -> Google assertion -> single-use
DEVICE_BOOTSTRAP continuation grant -> P-256 device challenge/signature -> server
OWNER membership/device -> signed ClientSession -> explicit administrative memory
scope -> contextual query. Scope is registered AFTER legitimate enrollment using
register_scope and synthetic information IDs; enrollment does not itself grant
memory access. Test-account revocation must revoke its sessions/memberships/scopes,
not merely remove the provider allowlist entry.

## Protected configuration and future authorization

The file selected by ANDY_LAB_CONFIG must be mode 0600, readable by runtime UID,
with fields environment=M2_SYNTHETIC_DEVICE, issuer_dsn, reader_dsn,
google_audience and allowed_subjects. DSNs target only the dedicated DB over the
Unix socket. Account sub values, passwords and actual endpoints stay outside Git.
Use a separate Google OAuth project/client and explicitly approved test account;
configure the Android .lab package and approved APK signing certificate in that
project. Do not reuse productive OAuth configuration or signing material.
Android needs labAttentionRouterBaseUrl, labGoogleWebClientId (same audience) and
labMemoryScopeRef. A blank-config CI APK is not the configured operational APK.
First enroll, register scope administratively, then rebuild/configure the lab
artifact with the resulting reference; revalidate its exact SHA, signature and hash.

compose.yaml/nginx.conf are INERT templates, not certified deployment artifacts.
No published port or automatic restart. The dedicated Dockerfile copies the
experimental sources omitted by the productive Dockerfile; supply PYTHON_IMAGE
with the approved Python 3.13 Slim digest and SOURCE_SHA with the exact commit.
Building requires dependency downloads on CI; no build was executed on the control
plane. The resulting image still requires its own build/smoke certification. All image variables must be immutable
approved digests; core image must contain this exact source and existing locked
dependencies. Socket volume UID/GID permissions, PostgreSQL HBA scram credentials,
bootstrap admin removal, and protected TLS file ownership must be prepared and
validated in disposable infrastructure before an operational approval. In particular,
provision the shared directories for PostgreSQL and UID 10001; Docker defaults do
not establish those permissions. Do not weaken modes to work around this precondition.

Communication graph: Android initiates HTTPS to restricted TLS ingress (future
approved binding); TLS initiates HTTP on core Unix socket; core initiates SQL on
PostgreSQL Unix socket. Core additionally needs ONLY Google certificate retrieval
through an approved destination-restricted proxy. The internal bridge does not
supply Internet egress: the proxy/egress policy and ingress binding are deliberately
not supplied or activated. Default operation therefore fails closed. No core/db
port is published. A private operational overlay must define the allowlisted ingress
and bounded certificate egress, explicitly deny host/production destinations and
prove that policy. Never attach production networks. L2 identity, dedicated/static
IP and VLAN are unnecessary; use experimental service identity and DNS. Internal
Docker networking alone is NOT proof of host isolation.

Required future acceptance: verify exact images/source/APK, isolate new persistence,
validate TLS hostname/CA and source restriction, prove host/production denial without
invasive probes, enroll only the test account/device, verify OWNER scope, query
persisted synthetic evidence, revoke and replay negative cases. Capture sanitized
correlation/evidence/revision receipts. Stop on missing gate/configuration/capacity.
Rollback removes only the separately authorized M2 resources, never M0.

This advances the owner's Android cockpit without expanding cognition. Live track:
authenticated synthetic current-memory query. Historical track: V0 revisions remain
intact; real history/import is intentionally deferred and outside authorization.
