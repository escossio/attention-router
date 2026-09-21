# Gmail Product Runner V1

The runner added in PR #146 executes one bounded poll for an existing
`ProviderAuthorization`, hardened in PR #147. The next increment adds a durable
incremental history cursor; the subscriber continues to use the normal Android
**Connect Gmail** flow.

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
Automatic invocation is provided by the governed Gmail scheduler merged in
PR #152; the durable incremental history primitive is described below.

## Authority and secrets

Before provider I/O the runner checks ACTIVE GOOGLE/GMAIL authorization with
exactly one allowed profile: `gmail.metadata` or `gmail.readonly`. Any mixed
or broader scope set fails closed. It also validates the active `channel.email`
binding, tenant, audience, canonical slot/instance identity, account fingerprint,
and the referenced unrevoked inbound credential. Credential validity is inclusive at `not_before`
and exclusive at `expires_at`; naive database timestamps are interpreted as UTC.
Each run reloads all three persisted rows without autoflush; an older entry in
the caller session's identity-map cache cannot authorize a run. This is an
admission-time validation; neutral ingress separately revalidates its credential
when receiving each event.

`ProviderSecretCipher` is the only decryption boundary. The runner reuses
`GmailConnectionService`'s AAD calculation, decrypts the refresh token and ingress
bearer only in memory, and checks the bearer against the credential digest.
The refresh POST contains only `grant_type=refresh_token`, `refresh_token`,
`client_id` and `client_secret`. Its timeout defaults to 10 seconds and cannot
exceed 30 seconds; the response is limited to 64 KiB and must be a JSON object
with a nonempty bearer access token. If scope is supplied, it must exactly match
the persisted authorization profile used for that run; lifetime must remain
positive. A readonly installation cannot silently refresh into metadata-only or
a broader scope.

The access token exists only for the current reader. Secrets are excluded from
token-bearing value-object repr, public results and runner errors. Error
boundaries discard underlying causes and contexts, including provider bodies,
transport exceptions and untrusted ingress error codes. HTTP redirects are
refused so credentials cannot follow a redirect to another endpoint.

## Poll and ingress

The reader lists `labelIds=INBOX` and never Gmail `q`. Metadata-only
installations keep the original path: `format=metadata` with only
From/To/Cc/Bcc/Subject/Date headers, no body, snippet, MIME parts or attachment
bytes, and events preserve `body_observed=false` plus
`attachments_observed=false`.

When attachment ingestion is explicitly enabled and the persisted authorization
is exactly `gmail.readonly`, the reader performs a second message request using
a server-side fields projection limited to MIME type, filename,
`body(attachmentId,size)`, and recursively bounded child parts. It never asks
for snippet or `body.data`; receiving body data anyway fails closed. Attachment
bytes are then fetched only through `users.messages.attachments.get`.

There are still no send, modify, mark-as-read or label mutation requests.

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
| `GMAIL_ATTACHMENT_INGESTION_ENABLED` | Defaults false; requires runner + Artifact Store and exact persisted `gmail.readonly`. |
| `GMAIL_ATTACHMENT_MAX_COUNT` | Default 10; hard limit 1..64 attachments per message. |
| `GMAIL_ATTACHMENT_MAX_BYTES` | Default 25 MiB decoded bytes per attachment; hard ceiling 256 MiB and cannot exceed Artifact Store max when enabled. |
| `GMAIL_ATTACHMENT_MAX_TOTAL_BYTES` | Default 32 MiB decoded bytes per message; bounded by per-attachment limit and 256 MiB ceiling. |
| `GMAIL_ATTACHMENT_MAX_MIME_DEPTH` | Default 12; hard limit 1..32. |

The ingress route is `/api/v1/ingress/integrations/events` on the ingress
application. `.env.example` names `http://ingress:18101` for the Compose network;
the Settings loopback default supports co-located processes. A server operator
must select the address reachable from the runner's network. Canary
`GMAIL_CONNECTOR_*` and `ATTENTION_ROUTER_INTEGRATION_*` variables are not read
by the product runner. No subscriber configuration or second provisioning flow
is introduced.

Attachment ingestion is separately default-off. `GMAIL_ATTACHMENT_INGESTION_ENABLED`
requires the product runner and Artifact Store. Count, decoded bytes per attachment,
decoded bytes per message and MIME depth are independently bounded by
`GMAIL_ATTACHMENT_MAX_COUNT`, `GMAIL_ATTACHMENT_MAX_BYTES`,
`GMAIL_ATTACHMENT_MAX_TOTAL_BYTES` and `GMAIL_ATTACHMENT_MAX_MIME_DEPTH`.
The per-attachment bound cannot exceed the Artifact Store object bound when the
feature is enabled.

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

## Durable incremental history (0046)

`GmailProductRunner.run_incremental(session, installation_id=..., max_results=...,
max_pages=10)` is the bounded execution primitive used by the automatic scheduler.
`run_once` retains its manual INBOX listing behavior. Android Connect Gmail remains
the sole subscriber installation flow. The scheduler is documented later in this
file and remains runtime-gated/default-off.

The nullable `provider_authorizations.gmail_history_id` belongs to the exact
installation row, with its tenant, human and account identity. Existing rows
migrate to NULL. Downgrade refuses to discard populated cursors with
`GMAIL_HISTORY_DOWNGRADE_REQUIRES_DATA_EXPORT`. **First execution reads only `/profile?fields=historyId`, stores
the current baseline and ingests zero messages. It does not backfill existing
mail.** The caller must commit that baseline. A new installation always seeds;
same-account reconnect preserves the cursor, while account replacement clears it
under the installation lock, including A -> B -> A replacement.

Subsequent calls use `users.history.list` with `historyTypes=messageAdded`,
`labelId=INBOX`, `maxResults=100` and a fields projection. Both exact
authorization profiles can drive the same durable cursor: `gmail.metadata`
keeps metadata-only ingestion, while `gmail.readonly` may additionally stage
attachments when the attachment feature is explicitly enabled. See the
[official history contract](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/history/list).
Only `messagesAdded` entries explicitly carrying INBOX are selected. Other
changes can advance the cursor without ingestion. History IDs must increase;
malformed or out-of-order responses fail closed. Message IDs are deduplicated
within the cycle. No legacy event DTO or dispatcher change is introduced.

Each cycle permits 1..100 selected messages, 1..10 pages, at most 100 history
records per page and at most 1 MiB per provider response. Each record is processed
whole. When the remaining message budget cannot cover the next record, execution
returns successfully with the last fully processed record as its cursor. A
record exceeding the configured message budget fails with
`GMAIL_PRODUCT_HISTORY_RECORD_TOO_LARGE`; it requires an explicitly larger bound
(up to 100) or future recovery tooling, never a skip. At the page bound only fully
examined records advance; mailbox-wide `historyId` is used only on the final
page. Repeated/invalid pagination tokens fail closed. History 404 yields
`GMAIL_PRODUCT_HISTORY_STALE` and **never reseeds automatically**. Recovery is an
explicit future workflow; stale history can represent lost provider retention.

### Transactions and replay

Use a clean caller-owned session and commit or rollback promptly after execution.
The primitive first reads the immutable slot without locking, then acquires a
PostgreSQL transaction-scoped advisory gate with `pg_try_advisory_xact_lock`.
It retains the installation `FOR UPDATE NOWAIT` lock and reloads governed
state after the gate. Concurrent runs fail with `GMAIL_PRODUCT_HISTORY_BUSY`.
Connect (including first connect and account replacement) and disconnect acquire
the same gate in waiting mode **before** any Tenant/ProviderAuthorization/Binding/
Credential row locks. Connect performs provider exchange/profile I/O before the
gate; disconnect performs revocation before touching binding/credential rows.
All locks release on caller commit/rollback; there are no hidden commits.

The key is the first eight SHA-256 bytes of
`attention-router:gmail-slot:v1:` plus the canonical persisted slot, interpreted
as a signed big-endian 64-bit integer in PostgreSQL's bigint advisory namespace.
The slot depends only on tenant/human/provider/product identity, not secrets,
account identity, cursor or installation existence. First connect computes the
same slot used by later runners. Hash collisions can over-serialize unrelated
operations or yield BUSY; they never confer authority. Other advisory namespaces
are domain-separated, though a 64-bit collision remains theoretically possible.

This order prevents reconnect from retaining Tenant while waiting for a runner
that is awaiting ingress. That original application-level cycle is partly HTTP,
so PostgreSQL's deadlock detector cannot see the full graph; ingress instead
hits its lock timeout. The runner holds no Tenant/Binding/Credential locks across
network I/O. Neutral ingress independently locks and revalidates its authority.
Use clean caller sessions without pre-acquired locks or pending writes; composing
these operations after unrelated locks can violate this ordering. SQLite's gate
is a functional no-op and does not certify concurrency.

Only successful cycles flush a new cursor; failures leave it at the cycle's
starting position, even if earlier events were admitted. There is no hidden
commit. A caller rollback also rolls back cursor advancement. HTTP admission is
independently durable and cannot be rolled back by this transaction.

Incremental events pass the immutable provider message timestamp as the replay
`received_at` value, so retrying unchanged metadata produces identical event
bytes. The authoritative actual receipt time remains the neutral inbox's
`admitted_at`. Manual `run_once` still uses the observation wall clock. Mixing
manual and incremental admission for the same message/binding can therefore
produce an idempotency conflict; it fails closed rather than advancing. Changed
provider metadata can likewise conflict and requires explicit investigation.
No new timestamp or duplicate semantics are imposed on neutral ingress.

Successful results expose only installation ID, initialization status, record
count, selected/accepted/duplicate counts and whether the cursor advanced.
Provider IDs, page tokens and all secrets stay out of result/repr and sanitized
error chains. Message bodies and snippets are never fetched. Attachment bytes
are fetched only for exact `gmail.readonly` installations when attachment
ingestion is explicitly enabled; those bytes are staged into Artifact Plane and
never serialized into the canonical e-mail event.


## Automatic polling scheduler V1

Issue #151 adds a dedicated automatic polling runtime around
`GmailProductRunner.run_incremental()`. Provider I/O is deliberately kept out of
the core Attention Router worker loop so a slow Gmail request cannot delay
decision, outbox, timer or personal-context work.

The runtime entry point is:

```text
python -m attention_router.infrastructure.gmail_scheduler
```

It remains inert unless `GMAIL_PRODUCT_SCHEDULER_ENABLED=true`. Enabling the
scheduler also requires the governed product runner to be enabled; the existing
runner requirement in turn requires the normal Gmail Connect boundary.

Each cycle first opens a short discovery session, selects a bounded rotating page
of ACTIVE GOOGLE/GMAIL installation IDs and closes that session before provider
I/O. Every selected installation then receives its own clean SQLAlchemy session
and transaction. A successful incremental call is committed immediately.
BUSY, STALE, authorization races, provider/ingress failures and unexpected
exceptions roll back only that installation.

Discovery uses the stable installation ID as a rotating cursor and wraps at the
end of the eligible set. This prevents a fixed first page from starving later
installations when the account count exceeds the configured batch size.

`GMAIL_PRODUCT_HISTORY_BUSY` is ordinary contention and does not fail the cycle.
`GMAIL_PRODUCT_HISTORY_STALE` is fail-closed: the scheduler never reseeds a
cursor. The installation is quarantined for the lifetime of that scheduler
process so repeated cycles do not hammer the provider with a known stale cursor.
A process restart may retry the stale installation once; durable stale-history
recovery remains a separate explicit operational workflow.

The scheduler logs only installation IDs, stable runner error codes and unexpected
exception type names. It does not log provider exception text, response bodies,
tokens, decrypted secrets or integration bearers.

| Setting | Default | Contract |
| --- | ---: | --- |
| `GMAIL_PRODUCT_SCHEDULER_ENABLED` | `false` | Automatic invocation remains opt-in. |
| `GMAIL_PRODUCT_SCHEDULER_POLL_INTERVAL_SECONDS` | `30` | Cycle interval, bounded to 1..3600 seconds. |
| `GMAIL_PRODUCT_SCHEDULER_BATCH_SIZE` | `20` | Active installations selected per cycle, bounded to 1..500. |
| `GMAIL_PRODUCT_SCHEDULER_MAX_PAGES` | `10` | Gmail history pages per installation/cycle, bounded to 1..10. |
| `GMAIL_PRODUCT_RUNNER_MAX_RESULTS` | `5` | Existing selected-message bound reused by incremental runs. |

This increment does not add a Compose service, enable runtime flags, deploy the
scheduler or make a live Gmail call. Deployment topology remains an explicit
operator decision after repository certification.
