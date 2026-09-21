# Gmail Product Runner V1

The runner added in PR #146 executes one bounded poll for an existing
`ProviderAuthorization`. This increment hardens that implementation; the
subscriber continues to use the normal Android **Connect Gmail** flow.

```text
Android Connect Gmail -> GmailConnectionService -> ProviderAuthorization
  -> GmailProductRunner.run_once(installation_id=...)
  -> refresh exchange -> GmailApiReader -> GmailInboundConnector
  -> /api/v1/ingress/integrations/events
```

`installation_id` is the exact persisted `ProviderAuthorization.id`. The server
caller supplies an SQLAlchemy session; the runner does not commit, provision an
installation or accept manually copied provider/integration tokens. The existing
one-shot script is an operator entry point, not a prerequisite for subscribers.
Scheduling, durable history cursors and automatic invocation remain future work.

## Authority and secrets

Before provider I/O the runner checks ACTIVE GOOGLE/GMAIL authorization with
exactly `gmail.metadata`, its active `channel.email` binding, tenant, audience,
canonical slot/instance identity, account fingerprint, and the referenced
unrevoked inbound credential. Credential validity is inclusive at `not_before`
and exclusive at `expires_at`; naive database timestamps are interpreted as UTC.
Each run reloads all three persisted rows without autoflush; an older entry in
the caller session's identity-map cache cannot authorize a run. This is an admission-time validation;
neutral ingress separately revalidates its credential when receiving each event.

`ProviderSecretCipher` is the only decryption boundary. The runner reuses
`GmailConnectionService`'s AAD calculation, decrypts the refresh token and ingress
bearer only in memory, and checks the bearer against the credential digest.
The refresh POST contains only `grant_type=refresh_token`, `refresh_token`,
`client_id` and `client_secret`. Its timeout defaults to 10 seconds and cannot
exceed 30 seconds; the response is limited to 64 KiB and must be a JSON object
with a nonempty bearer access token. If scope or lifetime is supplied, it must
remain metadata-only and positive respectively.

The access token exists only for the current reader. Secrets are excluded from
token-bearing value-object repr, public results and runner errors. Error
boundaries discard underlying causes and contexts, including provider bodies,
transport exceptions and untrusted ingress error codes. HTTP redirects are
refused so credentials cannot follow a redirect to another endpoint.

## Poll and ingress

The existing reader lists `labelIds=INBOX`, never Gmail `q`, and reads each
selected message with `format=metadata` and only From/To/Cc/Bcc/Subject/Date
headers. It does not inspect body, snippet, MIME parts or attachment bytes.
Events preserve `body_observed=false` and `attachments_observed=false`.
There are no send, modify, mark-as-read or label mutation requests.

`max_results` is an integer in 1..100 (default from configuration: 5). The
connector caps actual message reads and ingress submissions even if a reader
returns extra IDs. Each event uses the real binding's tenant, instance and
account reference. Neutral ingress owns idempotency: accepted and duplicate
responses contribute to their existing aggregate counters. A failure stops the
poll; earlier admitted events are not rolled back, and a later retry uses the
same ingress duplicate contract.

## Server configuration

Reuse the product settings already introduced by PR #146:

| Setting | Contract |
| --- | --- |
| `GMAIL_PRODUCT_RUNNER_ENABLED` | Defaults false; no flag is enabled by this increment. |
| `GMAIL_PRODUCT_RUNNER_MAX_RESULTS` | Default 5; hard limit 1..100. |
| `GMAIL_PRODUCT_RUNNER_INGRESS_URL` | Explicit server destination for neutral ingress. |
| `GOOGLE_WORKSPACE_OAUTH_CLIENT_ID`, `GOOGLE_WORKSPACE_OAUTH_CLIENT_SECRET` | Existing server OAuth client used by Connect Gmail. |
| `PROVIDER_AUTHORIZATION_KEY_B64URL` | Existing server AES-GCM key. |

The ingress route is `/api/v1/ingress/integrations/events` on the ingress
application. `.env.example` names `http://ingress:18101` for the Compose network;
the Settings loopback default supports co-located processes. A server operator
must select the address reachable from the runner's network. Canary
`GMAIL_CONNECTOR_*` and `ATTENTION_ROUTER_INTEGRATION_*` variables are not read
by the product runner. No subscriber configuration or second provisioning flow
is introduced.

## Stable failures

| Code | Meaning |
| --- | --- |
| `GMAIL_PRODUCT_RUNNER_DISABLED` | Runner/key configuration is unavailable. |
| `GMAIL_PRODUCT_AUTHORIZATION_UNAVAILABLE` | Authorization is absent or inactive. |
| `GMAIL_PRODUCT_AUTHORIZATION_INVALID` | Provider/product/scopes are invalid. |
| `GMAIL_PRODUCT_BINDING_INVALID` | Binding, credential, identity or bearer digest is invalid. |
| `GMAIL_PRODUCT_SECRET_INVALID` | Envelope/key/AAD or decrypted secret is invalid. |
| `GMAIL_PRODUCT_REFRESH_FAILED` | Refresh request or token response failed. |
| `GMAIL_PRODUCT_PROVIDER_UNAVAILABLE` | Reader construction, metadata read or normalization failed. |
| `GMAIL_PRODUCT_INGRESS_FAILED` | Neutral ingress construction or submission failed. |

Invalid polling limits raise `ValueError("GMAIL_POLL_LIMIT_OUT_OF_RANGE")` before
decryption or external I/O. Public successful results contain only installation
ID, binding ID and selected/accepted/duplicate counts.

Validation uses synthetic Connect Gmail persistence, real reader/connector
composition and fake HTTP responses, including explicit secret sentinels.
No real Gmail, live runtime, deployment or merge is required for these tests.
