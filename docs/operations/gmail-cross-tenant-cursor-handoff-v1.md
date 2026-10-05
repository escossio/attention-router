# Gmail cross-tenant cursor handoff V1

## Purpose

Move Gmail live continuity from one already-authorized tenant slot to another
tenant slot for the same authenticated Human Identity and the same Gmail account
without reseeding the destination from the provider.

This is an operator-controlled continuity operation. It does not move historical
application evidence between tenants and it does not grant tenant membership.

## Safety invariant

The handoff copies one existing `gmail_history_id` only when all of the following
are true:

- source and destination are distinct ProviderAuthorization installations;
- both are GOOGLE/GMAIL;
- source and destination belong to different tenants;
- both belong to the same Human Identity;
- both have the same provider account hash;
- source is REVOKED and its integration binding/credential are inactive/revoked;
- destination is ACTIVE with an active, unexpired inbound credential;
- source has a structurally valid Gmail history cursor;
- destination cursor is NULL, or already equals the source cursor for idempotent
  replay.

Both Gmail slot advisory locks are acquired in deterministic order before
ProviderAuthorization rows are locked or authority state is inspected.

The source cursor is preserved. The operation copies the exact value; it does not
increment, synthesize, query Gmail or clear source history.

## Operator CLI

Dry-run is the default:

```text
python scripts/handoff_gmail_cursor.py \
  --source-installation <revoked-source-installation> \
  --destination-installation <active-destination-installation>
```

Apply requires an explicit flag:

```text
python scripts/handoff_gmail_cursor.py \
  --source-installation <revoked-source-installation> \
  --destination-installation <active-destination-installation> \
  --apply
```

The CLI never prints the cursor, refresh token, integration bearer, OAuth access
token or provider response text. Stable refusal codes are returned instead of
exception details.

## Recommended live cutover order

Google documents programmatic token revocation as removing the project's OAuth
authorization for the user and invalidating issued access/refresh tokens. A new
destination grant must therefore be created **after** source revocation, not
before it:

https://developers.google.com/identity/protocols/oauth2/web-server#tokenrevoke

1. Confirm the authenticated Human Identity is an ACTIVE member of both the
   current source tenant and intended destination tenant.
2. Stop Channel Sync and prove no legacy Gmail scheduler is running.
3. Record only non-secret source/destination installation references needed by
   the operator. Preserve the source ProviderAuthorization and history cursor.
4. With the Client Session on the source tenant, disconnect Gmail. This revokes
   the Google project authorization and deactivates the source binding/credential
   while preserving the durable source cursor.
5. Switch the Android Client Session to the destination tenant through the normal
   device challenge/signature flow.
6. Connect Gmail in the destination tenant. The current Android client requests
   `gmail.readonly`; if Google does not return a refresh token on the first
   attempt, the existing explicit-consent retry remains authoritative.
7. Keep Channel Sync stopped. The destination must remain unpolled so its cursor
   stays NULL.
8. Run the handoff CLI without `--apply`. Continue only when it returns `READY`.
9. Run the same command with `--apply`. `APPLIED` or `ALREADY_APPLIED` is success.
10. Start Channel Sync with the destination installation discoverable.
11. Observe the first incremental cycle. The runner must continue from the copied
    cursor; it must not execute first-run baseline initialization.
12. Keep the revoked source ProviderAuthorization row and cursor for
    audit/recovery.

The service gap between source revocation and destination activation does not
require a provider baseline reseed. Gmail messages that arrive during that gap
remain discoverable from the preserved source history cursor once the destination
installation resumes incremental history processing.

If destination connection fails after source revocation, keep Channel Sync
stopped and retry the normal destination consent flow. Do not initialize the
destination with a fresh baseline and do not manually invent a historyId.

## Rollback

Before source disconnect, rollback is simply to keep the original installation
and resume Channel Sync.

After source disconnect, the prior Google project authorization has been revoked.
Rollback therefore requires a fresh, explicitly authorized OAuth connection in
the chosen tenant. Never restore service by manually inventing or reseeding a
historyId.

A destination cursor already copied by this operation is not deleted by rollback.
It remains evidence of the attempted continuity handoff.

## Live continuity impact

This feature exists specifically to preserve Track A live continuity across a
tenant-slot move. It prevents the destination's first Channel Sync run from
treating an already-observed mailbox as a brand-new baseline and silently
skipping messages that arrived during cutover.

The operation itself performs no provider I/O.

## Historical acceleration impact

No historical acceleration is implemented here. Existing source
`integration_inbox`, CanonicalEvent, Timeline, ConversationMessage or Memory rows
remain in their original tenant and retain their provenance.

Moving or importing historical evidence across tenant boundaries is a separate
governed problem. This cursor handoff must not be interpreted as permission to
rewrite that history.

## Non-goals

- tenant membership creation;
- Android tenant-selection UX;
- source actor/entity resolution;
- historical Gmail backfill;
- memory/context enablement;
- attachment enablement;
- changing OAuth credentials;
- changing Neutral Integration Ingress authority.
