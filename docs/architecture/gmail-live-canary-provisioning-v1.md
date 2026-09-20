# Gmail Live Canary Provisioning V1

Status: implementation candidate

Related:
- #83 Multi-channel expansion
- Gmail Connector V1
- Gmail API Reader + Canary V1
- Neutral Integration Ingress V1
- Neutral Integration Dispatch V1

## Purpose

Provide the operational provisioning boundary required before the first
one-message live Gmail canary without storing Gmail OAuth credentials inside
Attention Router or enabling production polling.

Two independent credentials are required:

1. **Gmail access token**
   - issued by Google;
   - intended scope: `gmail.readonly`;
   - runtime-only;
   - remains outside Attention Router persistence.

2. **Attention Router neutral-ingress bearer**
   - generated locally for one integration installation;
   - bound to one tenant + `channel.email` instance;
   - raw secret written once to a mode-0600 file;
   - only the SHA-256 digest is stored in PostgreSQL.

These credentials are not interchangeable.

## Atomic Attention Router installation

Administrative helper:

`provision_installation(session_factory, binding, credential)`

creates the first integration binding and credential in one PostgreSQL
transaction.

It requires:

- existing ACTIVE tenant;
- exact credential -> binding identity match;
- exact scope match;
- unused binding ID;
- unused audience/tenant/kind/name/instance/account namespace.

A conflict rolls back both rows.

Credential rotation remains a separate operation through the existing
`provision_credential()` API.

## Gmail canary provisioner

CLI:

`scripts/provision_gmail_canary.py`

Default behavior is dry-run.

Example dry-run:

```bash
python scripts/provision_gmail_canary.py \
  --tenant-id <TENANT_ID> \
  --instance-id gmail-primary \
  --account-id <MAILBOX_ACCOUNT_ID>
```

Dry-run:

- does not change PostgreSQL;
- does not create a secret file;
- prints only non-secret installation metadata.

Actual provisioning requires `--apply` and an **absolute** secret-file path:

```bash
python scripts/provision_gmail_canary.py \
  --tenant-id <TENANT_ID> \
  --instance-id gmail-primary \
  --account-id <MAILBOX_ACCOUNT_ID> \
  --secret-file /run/secrets/attention-router-gmail-canary.env \
  --apply
```

The secret file is:

- created with create-exclusive semantics;
- never overwritten;
- chmod 0600;
- removed if PostgreSQL provisioning fails;
- written as:
  `ATTENTION_ROUTER_INTEGRATION_BEARER=<secret>`.

The raw bearer is never printed by the CLI and is excluded from object reprs.

Default credential lifetime:

24 hours.

Allowed range:

1..168 hours.

## OAuth requirement

The canary Gmail reader requires a Google OAuth access token with read-only
Gmail access.

The intended scope is:

`https://www.googleapis.com/auth/gmail.readonly`

For a canary, obtain the token through a Google OAuth client outside Attention
Router. Google client ID/secret, refresh token and consent artifacts are not
stored in the Attention Router database.

The Gmail API must be enabled for the Google Cloud project used by that OAuth
client.

Broader/public deployment can trigger Google's OAuth verification/security
requirements for restricted Gmail scopes. That provider-governance work is
outside this canary increment.

## Canary runtime inputs

The one-shot Gmail canary script requires:

- `GMAIL_ACCESS_TOKEN`
- `GMAIL_CONNECTOR_TENANT_ID`
- `GMAIL_CONNECTOR_INSTANCE_ID`
- optional `GMAIL_CONNECTOR_ACCOUNT_ID`
- `ATTENTION_ROUTER_INTEGRATION_INGRESS_URL`
- `ATTENTION_ROUTER_INTEGRATION_BEARER`

No token should be placed in repository files.

## Runtime gates

A build containing the merged integration stack must be deployed into the
chosen canary environment.

The following runtime controls must be explicitly enabled for that canary:

- `INTEGRATION_INGRESS_ENABLED=true`
- `INTEGRATION_DISPATCH_ENABLED=true`

The configured ingress audience must equal the provisioned binding audience.

This provisioning PR does **not** enable those flags or deploy any runtime.

## One-message canary

Default command:

```bash
python scripts/gmail_integration_canary.py --max-results 1
```

Default Gmail query:

`in:inbox newer_than:1d -in:spam -in:trash -has:attachment`

The canary:

- lists at most one matching message by default;
- reads it without Gmail mutation;
- rejects attachment-bearing messages;
- normalizes through `EmailNormalizedAdapter`;
- POSTs to the neutral ingress;
- accepts 202 or idempotent 200 duplicate;
- prints aggregate counts only.

## Read-only verification

Verification script:

`scripts/verify_gmail_canary.py --binding-id <BINDING_ID>`

It verifies:

- at least one integration receipt exists;
- every canary receipt is terminal;
- no BLOCKED/PENDING receipt remains;
- each PROCESSED receipt has exactly one CanonicalEvent;
- each canonical event has exactly one TimelineEvent.

It prints counts only and does not expose provider-private identifiers.

## Rollback / containment

If the canary needs to stop:

1. stop the one-shot process;
2. disable the integration binding or disable ingress/dispatch;
3. revoke the neutral integration credential;
4. discard the Gmail access token/provider session outside Attention Router.

No Gmail mailbox mutation needs to be undone because the canary is read-only.

## Hard boundary

This increment does not:

- deploy a new runtime;
- enable ingress/dispatch flags;
- provision a real production tenant;
- obtain or store a real Gmail token;
- create a Google OAuth client;
- start an always-on poller;
- download attachments;
- mutate Gmail;
- enqueue the Decision Engine;
- send e-mail.

## Proof

Tests cover:

- bounded 1..168 hour credential lifetime;
- random raw bearer -> digest-only DB record;
- raw bearer excluded from repr;
- create-once mode-0600 secret file;
- no overwrite;
- secret cleanup on DB provisioning failure;
- atomic binding+credential PostgreSQL installation;
- mismatch rejection without partial rows;
- namespace conflict rollback.
