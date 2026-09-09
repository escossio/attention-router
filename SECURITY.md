# Security policy

## Supported code

There is no stable public release or security-support SLA yet. The supported public branch is `main`; historical and staging branches are not supported deployment releases. This repository is a prerelease and security findings should be reported privately before any public disclosure.

## Private reporting and coordinated disclosure

Do not report exploitable details in a public issue or pull request. Use GitHub private vulnerability reporting **only when it is enabled**. The public reporting channel is not yet configured; until then, request a private channel from the maintainer without disclosing exploit details. Configuring and testing that channel is a publication gate.

Include affected revision, impact and a minimal synthetic reproduction. Coordinate disclosure while the issue is assessed and remediated. Do not test against other people's accounts or a live deployment without permission. No response-time guarantee is currently offered.

## Handling sensitive material

Never include API keys, tokens, session cookies, phone numbers, private conversations, media, database dumps or unredacted logs in issues, commits or CI output. Revoke/rotate exposed credentials first; deleting text from a branch does not invalidate a credential or erase cached copies.

Secret scanning is a release gate, not proof that every sensitive datum has been found. Narrowly documented generated-test-ID exceptions must not become broad secret allowlists. CodeQL is an active required security check; a skipped or unavailable analysis is blocked, not passed.

## Security boundaries

Agent output is a proposal, not execution authority. Policies, Owner Control, human approval, idempotency and delivery evidence are separate controls. Tenant/contact scope and memory disclosure limit what enters context. Ambiguous effects require investigation rather than blind replay.

The Quick Start binds the API to loopback, does not expose PostgreSQL on the host and disables provider-backed functionality by default. Its public example credentials are not production credentials. Authentication, secrets management, network controls and service configuration must be reviewed for any deployment.

See [architecture](docs/architecture/overview.md) for trust and control boundaries.
