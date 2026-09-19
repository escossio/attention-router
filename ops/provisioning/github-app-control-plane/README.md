# GitHub App control-plane provisioning

This package reproduces the AGT-side ingress proven live for the `andy-github-control-plane` GitHub App.

The initial installation is intentionally scoped to `escossio/attention-router`. The GitHub App must explicitly subscribe to **Pull request**, **Workflow run**, and **Check run** events; the receiver accepts `ping`, `pull_request`, `workflow_run`, and `check_run` deliveries. It records bounded metadata and may advance a pre-registered AGT-local continuation stage, but it does not commit, merge, rerun workflows, deploy, execute shell commands, or mutate repository state.

## Trust boundary

```text
GitHub App
  -> HTTPS webhook
  -> Cloudflare Tunnel path route
  -> 127.0.0.1:18104
  -> HMAC-SHA256 verification
  -> repository + installation allowlist
  -> delivery-id deduplication
  -> SQLite metadata receipt
  -> exact stage correlation (repo + PR + SHA + event kind + target name)
  -> monotonic local completion signal
```

The webhook receiver does not load the GitHub App private key. Systemd exposes only the webhook secret through `LoadCredential=`. The private key is reserved for short-lived App JWT / installation-token operations such as the provisioning probe.

## Host layout

Expected host-local paths:

```text
/etc/andy-github-app/app.env
/etc/andy-github-app/installation.env
/etc/andy-github-app/credentials/private-key.pem
/etc/andy-github-app/credentials/webhook-secret
/usr/local/lib/andy-github-app/webhook_receiver.py
/usr/local/lib/andy-github-app/continuation_control.py
/var/lib/andy-github-app/events.sqlite3
```

The credential directory should be `0700 root:root`; the private key and webhook secret should be `0600 root:root`.

Copy `app.env.example` to the host configuration and place the private key outside Git. Run `probe.py` locally to authenticate the App, discover the single installation owned by `escossio`, prove read-only repository access, and write only the non-secret Installation ID/owner to `installation.env`.

The probe keeps App JWT and installation token in memory and never prints either credential.

## Receiver installation

Install the receiver and unit:

```bash
install -d -m 0755 /usr/local/lib/andy-github-app
install -m 0755 webhook_receiver.py /usr/local/lib/andy-github-app/webhook_receiver.py
install -m 0755 continuation_control.py /usr/local/lib/andy-github-app/continuation_control.py
install -m 0644 andy-github-webhook.service /etc/systemd/system/andy-github-webhook.service
systemctl daemon-reload
systemctl enable --now andy-github-webhook.service
```

Local validation must pass before public routing:

```bash
curl --fail http://127.0.0.1:18104/health
curl -sS -o /dev/null -w '%{http_code}\n' \
  -X POST -H 'Content-Type: application/json' \
  -H 'X-GitHub-Event: ping' -H 'X-GitHub-Delivery: unsigned-test' \
  --data '{}' http://127.0.0.1:18104/github/webhook
```

The second command must return `401`.

## Cloudflare route

Use the path-specific rule in `cloudflared-ingress.example.yml` before the existing catch-all rule for the same hostname. Validate both the GitHub path and a legacy hook path before reloading the tunnel.

The public webhook URL used by the current deployment is:

```text
https://hooks.escossio.com/github/webhook
```

TLS verification stays enabled at GitHub.

## Event receipt

SQLite stores only bounded routing/status metadata:

- GitHub delivery ID;
- receive timestamp;
- event/action;
- repository and installation ID;
- PR number and exact head SHA when available;
- workflow run ID/name/status/conclusion when available;
- check run ID/name/status/conclusion when available.

Raw webhook bodies, private keys, signatures, tokens, conversations and provider credentials are not persisted.


## Event-driven continuation stages

A controller may pre-register a bounded stage that it is waiting for. Correlation is exact across:

- repository;
- pull request number;
- exact 40-character head SHA;
- event kind (`workflow_run` or `check_run`);
- target name (workflow name or check/job name).

For a whole workflow:

```bash
continuation_control.py register \
  --stage-id pr80-public-ci \
  --pr-number 80 \
  --head-sha <exact-head-sha> \
  --event-kind workflow_run \
  --target-name "Public CI"
```

For one gate such as PostgreSQL integration:

```bash
continuation_control.py register \
  --stage-id pr80-postgres \
  --pr-number 80 \
  --head-sha <exact-head-sha> \
  --event-kind check_run \
  --target-name postgres-integration
```

A matching signed GitHub completion moves the stage exactly once from `RUNNING` to one terminal state:

- `COMPLETED_SUCCESS`;
- `COMPLETED_FAILURE`;
- `CANCELLED`.

A stale SHA, wrong PR, wrong target name, replayed delivery, or later contradictory event cannot regress or advance the stage. Successful transitions emit one local `continuation_signals` row.

The next controller pass can read pending signals without polling GitHub:

```bash
continuation_control.py list-ready
continuation_control.py show pr80-postgres
continuation_control.py ack pr80-postgres
```

Acknowledgement marks only the local signal as consumed. It does not mutate GitHub and does not execute the next action by itself.

## Current authority split

GitHub Actions remains the repository-native CI control plane. Existing CI Agent / Copilot Advisor / bounded autofix jobs are not copied to AGT.

This package adds an independent ingress bridge plus bounded AGT-local continuation state. Trusted GitHub completions may mark a pre-registered local stage complete, but the resulting signal carries no repository or host mutation authority. Any future automatic next-step execution must be introduced separately, with exact-head validation and the repository's existing approval/safety gates.

The Remote Desktop Commander operator bridge is documented separately in `../agt-remote-access/`; it is an administrative transport, not an application dependency and not GitHub App authority.

## Rollback

Stop and disable the receiver, then remove only the path-specific Cloudflare ingress rule:

```bash
systemctl disable --now andy-github-webhook.service
cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate
```

Do not remove or rotate the GitHub App private key or webhook secret as part of a routing rollback unless credential rotation is the explicit objective.
