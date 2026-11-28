# M2 protected configuration — prepared, not activated

No account, client ID, certificate, key, password, address or firewall rule is
provisioned by this package. Empty example values deliberately cannot start it.

## OAuth and Android artifacts

Use a new experimental Google project and consent configuration restricted to the
explicitly approved test account. Create a web OAuth audience for backend ID-token
verification and an Android client tied to the `.lab` package plus the **stable
dedicated lab signer** fingerprints. Obtain the subject through the approved
identity enrollment procedure; never derive it from email or copy production
bindings. Both backend audience and Android `labGoogleWebClientId` must agree.
No productive project, signer, client, session or bearer is reused.

The first configured APK has an HTTPS lab origin and audience but no scope. After
legitimate enrollment, an administrator registers the OWNER-bound memory scope.
The second APK includes that scope, with exactly the same package and signer so
Keystore and session survive update. Certify both APK hashes and signatures. This
requires the Android #106 merge and #104 adaptation; a blank CI APK proves neither
configured artifact. No automatic scope-discovery endpoint is introduced.

## Protected runtime file and PostgreSQL

Copy config.example.json to a protected location OUTSIDE Git/build contexts. Owner
10001:10001, mode 0600, regular file. Set separate issuer/reader DSNs with driver
`postgresql+psycopg`, host `/socket`, and the dedicated
`andy_memory_m1_<32 hexadecimal characters>` database. Provision the SYNTHETIC_M1
marker, M1 schema/roles and four onboarding tables explicitly; no startup migration.
Use generated experimental passwords only during separately authorized provisioning.
Runtime roles are not owners/superusers and cannot create schemas or bypass RLS.
The existing service/ORM imports also instantiate global Settings. Supply independent
random lab_admin_secret and lab_ingress_secret (at least 32 characters) in that same
protected file. They satisfy the unchanged admin/HMAC guards; no admin/internal
ingress router is mounted and neither value is a ClientSession. The inherited ORM
engine points to in-memory SQLite; all actual identity/memory work uses the explicit
dedicated PostgreSQL factories. Do not set APP_ENV=test or disable those guards.
The reader cannot mutate memory/identity. The issuer writes identity only; memory
corrections are administrative operations outside HTTP. Recheck grants by SQL
catalog inspection in the disposable rehearsal.

`pg_hba.conf` permits OS-authenticated postgres administration and SCRAM for other
Unix-socket users; every IP connection is rejected. PostgreSQL also has no network
and no listen address. The pinned Alpine image uses postgres UID/GID 70: confirm
that identity before executing socket-init. Socket-init is a bounded, one-shot,
networkless initializer of ONLY the two new named volumes: postgres directory
70:70/2770, HTTP directory 10001:10001/2770. Core joins group 70; unprivileged TLS
joins group 10001. Never use 0777 or run the core as root to repair permissions.
No productive volume or Docker administrative socket is mounted.

## TLS and isolation release conditions

Select a dedicated approved lab hostname and certificate trusted by the physical
Android device. SAN/expiry/chain/private-key match must pass offline verification.
Store server.crt/server.key and allowed-device.conf outside Git, readable only by
the unprivileged TLS service (key 0440, suitable group 101); certificate keys never
enter the image. Source rules allow only the approved device/VPN, followed by
the template's unconditional deny. Do not add a public catch-all source rule.

The declarative graph is Android → TLS:8443 → Unix HTTP → core → Unix SQL → DB.
Only the core verifier needs outbound certificate retrieval. An independently
approved proxy must allow CONNECT only to the certificate host used by pinned
google-auth (`www.googleapis.com:443`), with private/link-local/host destinations
denied and bounded timeouts. Confirm the precise certificate URL from that pinned
library before granting egress; do not open general Internet access. The internal
bridge alone does not prove host isolation. The operational overlay must include
an explicit host/production deny policy plus restricted ingress, with an offline
review and later authorized packet-level proof. No overlay or binding is applied
by this task. No dedicated/static IP, L2 identity or VLAN is required.

Acceptance before activation: immutable runtime/base digests and source label;
dependency receipt; no unexpected mounts/capabilities/ports; uid/gid and directory
permissions; database/HBA/least privileges; TLS verification/source restriction;
proxy/host-denial evidence; valid experimental OAuth account/audience; exact APK
hash/package/signer. Missing evidence keeps the relevant gate closed. Restore by
removing only newly authorized M2 resources; never touch M0 or existing services.
