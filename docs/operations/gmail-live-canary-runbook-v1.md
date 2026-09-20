# Gmail Live Canary Runtime Runbook V1

Status: implementation candidate

Related:
- #83 Multi-channel expansion
- Neutral Integration Ingress V1
- Neutral Integration Dispatch V1
- Gmail Connector V1
- Gmail API Reader + Canary V1
- Gmail Live Canary Provisioning V1

## Objective

Run one isolated, read-only Gmail message through:

`Gmail -> neutral ingress -> integration_inbox -> CanonicalEvent -> TimelineEvent`

without touching the current Attention Router live stack.

## Isolation model

Use a separate Docker Compose project:

`attention-router-gmail-canary`

The canary stack contains only:

- PostgreSQL;
- one-shot Alembic migration;
- neutral ingress;
- worker;
- a profile-gated tools container used only for provisioning/canary commands.

It does not include:

- public API;
- internal ingress;
- WhatsApp ingress/browser transport;
- TTS/STT;
- provider delivery;
- Agent Decision Pipeline.

The only host port is:

`127.0.0.1:18201 -> ingress:18101`

The database uses a project-scoped Docker volume:

`gmail_canary_database`

No live database migration is required.

## Source checkout

Never use a dirty/stale live runtime directory as the build source.

Create a dedicated checkout from the exact approved main SHA:

```bash
git clone https://github.com/escossio/attention-router.git \
  /srv/projetos/attention-router-gmail-canary
cd /srv/projetos/attention-router-gmail-canary
git checkout <APPROVED_MAIN_SHA>
```

Verify:

```bash
git status --short
git rev-parse HEAD
```

Expected: clean checkout at the approved SHA.

## Step 1 — create private runtime env

```bash
python scripts/init_gmail_canary_env.py \
  --path /srv/projetos/attention-router-gmail-canary/.env.gmail-canary
```

This generates random:

- PostgreSQL password;
- internal-ingress HMAC placeholder required by Settings.

The file is created mode 0600 and never overwritten.

Important default state:

```text
INTEGRATION_INGRESS_ENABLED=false
INTEGRATION_DISPATCH_ENABLED=false
```

All execution/delivery/WhatsApp/Agent/Personal Context surfaces are also
disabled.

Transport readiness probes are pointed to loopback port 1 so the canary worker
does not probe the live WhatsApp transport.

## Step 2 — prepare local secret directory

```bash
mkdir -p secrets/gmail-canary
chmod 700 secrets/gmail-canary
```

The repository ignores `secrets/`.

## Step 3 — build isolated canary

```bash
docker compose \
  --project-name attention-router-gmail-canary \
  --env-file .env.gmail-canary \
  -f compose.gmail-canary.yaml \
  build
```

Do not deploy if the build fails.

## Step 4 — start with integration gates OFF

```bash
docker compose \
  --project-name attention-router-gmail-canary \
  --env-file .env.gmail-canary \
  -f compose.gmail-canary.yaml \
  up -d db migrate ingress worker
```

The migration service must complete successfully.

Verify:

```bash
docker compose \
  --project-name attention-router-gmail-canary \
  --env-file .env.gmail-canary \
  -f compose.gmail-canary.yaml \
  ps
```

Expected:

- db healthy;
- migrate exited 0;
- ingress healthy;
- worker healthy.

The neutral integration endpoint still returns 503 while the ingress flag is
false.

## Step 5 — create isolated default tenant

Run through the profile-gated tools image:

```bash
docker compose \
  --project-name attention-router-gmail-canary \
  --env-file .env.gmail-canary \
  -f compose.gmail-canary.yaml \
  --profile tools run --rm tools \
  python scripts/ensure_gmail_canary_tenant.py
```

Expected tenant:

`00000000-0000-4000-8000-000000000001`

This tenant exists only in the isolated canary database.

## Step 6 — provision channel.email installation

```bash
docker compose \
  --project-name attention-router-gmail-canary \
  --env-file .env.gmail-canary \
  -f compose.gmail-canary.yaml \
  --profile tools run --rm tools \
  python scripts/provision_gmail_canary.py \
    --tenant-id 00000000-0000-4000-8000-000000000001 \
    --instance-id gmail-canary \
    --audience andy-gmail-canary \
    --secret-file /run/secrets/gmail-canary/integration.env \
    --apply
```

The command prints:

- binding ID;
- credential ID;
- expiry;
- secret file path.

It never prints the raw bearer.

The secret file contains only:

`ATTENTION_ROUTER_INTEGRATION_BEARER=<opaque secret>`

and is mode 0600 on the host-mounted secret directory.

Record the returned `binding_id` for verification.

## Step 7 — obtain Gmail read-only access token externally

Use a Google OAuth client outside Attention Router with intended scope:

`https://www.googleapis.com/auth/gmail.readonly`

The Gmail API must be enabled for that Google Cloud project.

Do not store client secret, refresh token or Gmail access token in:

- Git;
- PostgreSQL;
- `.env.gmail-canary`;
- logs.

For the one-shot canary, export the access token only in the invoking shell.

## Step 8 — explicitly enable canary integration gates

This step is the activation boundary.

```bash
python scripts/set_gmail_canary_integration.py \
  --path /srv/projetos/attention-router-gmail-canary/.env.gmail-canary \
  --enable
```

Recreate only ingress + worker so they consume the new env:

```bash
docker compose \
  --project-name attention-router-gmail-canary \
  --env-file .env.gmail-canary \
  -f compose.gmail-canary.yaml \
  up -d --force-recreate ingress worker
```

Do not enable these flags in the live Attention Router stack.

## Step 9 — run exactly one Gmail message

Read the neutral-ingress bearer into the process environment without printing
it:

```bash
set -a
. ./secrets/gmail-canary/integration.env
set +a
```

Set the ephemeral provider token:

```bash
export GMAIL_ACCESS_TOKEN='<ephemeral gmail.readonly access token>'
```

Run one-message canary inside the tools container:

```bash
docker compose \
  --project-name attention-router-gmail-canary \
  --env-file .env.gmail-canary \
  -f compose.gmail-canary.yaml \
  --profile tools run --rm \
  -e GMAIL_ACCESS_TOKEN \
  -e ATTENTION_ROUTER_INTEGRATION_BEARER \
  tools \
  python scripts/gmail_integration_canary.py --max-results 1
```

Default query excludes attachment-bearing messages:

`in:inbox newer_than:1d -in:spam -in:trash -has:attachment`

Expected output contains counts only.

## Step 10 — verify end-to-end chain

```bash
docker compose \
  --project-name attention-router-gmail-canary \
  --env-file .env.gmail-canary \
  -f compose.gmail-canary.yaml \
  --profile tools run --rm tools \
  python scripts/verify_gmail_canary.py \
    --binding-id <BINDING_ID>
```

Expected:

```json
{
  "status": "PASS",
  "receipts": 1,
  "processed": 1,
  "pending": 0,
  "blocked": 0,
  "canonical_events": 1,
  "timeline_events": 1
}
```

A duplicate Gmail poll may produce an idempotent 200 response without creating
additional canonical/timeline rows.

## Step 11 — disable immediately after proof

```bash
python scripts/set_gmail_canary_integration.py \
  --path /srv/projetos/attention-router-gmail-canary/.env.gmail-canary \
  --disable
```

Then recreate ingress + worker:

```bash
docker compose \
  --project-name attention-router-gmail-canary \
  --env-file .env.gmail-canary \
  -f compose.gmail-canary.yaml \
  up -d --force-recreate ingress worker
```

Unset the provider token:

```bash
unset GMAIL_ACCESS_TOKEN
unset ATTENTION_ROUTER_INTEGRATION_BEARER
```

## Full teardown

To stop but preserve the isolated database for evidence:

```bash
docker compose \
  --project-name attention-router-gmail-canary \
  --env-file .env.gmail-canary \
  -f compose.gmail-canary.yaml \
  down
```

To destroy the isolated database only after evidence is no longer required:

```bash
docker compose \
  --project-name attention-router-gmail-canary \
  --env-file .env.gmail-canary \
  -f compose.gmail-canary.yaml \
  down -v
```

Never run `down -v` against the live project name.

## Success criteria

The first live canary is successful only if all are true:

1. Gmail query is read-only and bounded to one message;
2. connector returns accepted or duplicate;
3. exactly one integration receipt is terminal;
4. no receipt is BLOCKED/PENDING;
5. one canonical event exists;
6. one timeline event exists;
7. no Gmail state was mutated;
8. no live Attention Router container/database was changed;
9. no provider or neutral-ingress secret appears in logs/output;
10. integration gates are disabled again after the proof.

## Current AGT preflight

At the time this runbook was authored, the live stack was intentionally left
untouched and remained on its older runtime. Candidate loopback port 18201 was
available for the isolated canary.

Always re-run port/disk/container preflight immediately before deployment.
