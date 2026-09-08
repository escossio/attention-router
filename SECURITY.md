# Security policy

## Supported versions

This is a pre-release public-baseline candidate. No stable support window or response-time SLA is offered yet. A supported release policy will be published before the first maintained release. Known dependency risks and incomplete deployment validation must be resolved before production use.

## Report privately

The private security contact will be configured before public release. Once enabled, use GitHub private vulnerability reporting under this repository's Security tab. Until then, do not open a public issue containing exploit details or private data; request a private channel from the maintainer without including the vulnerability itself.

Never post API keys, tokens, browser sessions, real conversation content, phone numbers or database exports in issues, pull requests, screenshots or logs. Revoke exposed credentials immediately; deleting a file is not revocation.

## Coordinated disclosure

Provide affected version, impact and minimal synthetic reproduction privately. Coordinate a remediation and disclosure date with the maintainer. Do not access another person's messages, test against live users, perform disruptive testing or publish unredacted evidence.

## Boundaries

Agent output does not authorize execution. Owner controls, policies, review and execution intents govern effects. Tenant/contact isolation, signed internal ingress, idempotency and delivery proof are security boundaries. Ambiguous deliveries must remain fail-closed. TTS/STT and WhatsApp are separately configured integrations; their credentials and browser state never belong in this repository. Local example credentials are intentionally public and unsuitable for shared deployments.
