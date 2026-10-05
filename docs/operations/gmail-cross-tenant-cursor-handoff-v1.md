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

1. Confirm the authenticated Human Identity is an ACTIVE member of both the
   current source tenant and intended destination tenant.
2. Stop Channel Sync and prove no legacy Gmail scheduler is running.
3. Switch the Android Client Session to the destination tenant through the normal
   device challenge/signature flow.
4. Connect Gmail in the destination tenant. The destination must remain unpolled
   so its cursor stays NULL. A scope upgrade such as metadata -> readonly is
   allowed because provider account identity, not scope profile, anchors the
   cursor handoff.
5. Optionally inspect the new installation and account identity while the old
   source remains active. Do not restart Channel Sync.
6. Switch the Client Session back to the source tenant and disconnect Gmail there.
   This revokes the source provider grant and deactivates its binding/credential.
7. Switch the Client Session to the destination tenant again.
8. Run the handoff CLI without `--apply`. Continue only when it returns `READY`.
9. Run the same command with `--apply`. `APPLIED` or `ALREADY_APPLIED` is success.
10. Start Channel Sync with the destination installation discoverable.
11. Observe the first incremental cycle. The runner must continue from the copied
    cursor; it must not execute first-run baseline initialization.
12. Keep the source ProviderAuthorization row and cursor for audit/recovery.

If destination connection fails before source disconnect, leave the source
installation active and abort the cutover. If handoff fails after source
disconnect, keep Channel Sync stopped, correct the refusal condition, and retry;
do not allow the destination to initialize a fresh baseline.

## Rollback

Before source disconnect, rollback is simply to keep the original installation
and resume Channel Sync.

After source disconnect, rollback requires an explicitly authorized reconnect of
the source tenant. Never restore service by manually inventing or reseeding a
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
