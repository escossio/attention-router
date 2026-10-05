# Channel Sync V1 — live cutover checkpoint

Date: 2026-10-05

## Outcome

Channel Sync V1 completed its first successful live cutover for Gmail.

The dedicated Channel Sync process is now the active Gmail scheduler. The legacy
standalone Gmail scheduler was stopped before the successful Channel Sync run and
remained stopped during the validation window.

The live path proven was:

`Channel Sync -> Neutral Integration Ingress -> PostgreSQL admission`

The Neutral Integration Ingress admitted the Channel Sync requests with HTTP
`202 Accepted`. The first successful Channel Sync cycle reported:

```text
adapter=google.gmail
selected=1
processed=1
initialized=0
accepted=2
duplicates=0
cursor_advanced=1
busy=0
stale=0
quarantined=0
unavailable=0
failed=0
```

A following cycle completed with `failed=0` and no new work. The Channel Sync
and Neutral Integration Ingress containers were both healthy at the end of the
proof.

## Root cause of GMAIL_PRODUCT_INGRESS_FAILED

The incident had two distinct deployment causes.

### 1. Gmail targeted the wrong ingress surface

The live Gmail runtime was configured to send
`POST /api/v1/ingress/integrations/events` to the Internal Ingress listener.

The Internal Ingress application is a different surface. It does not expose the
provider-neutral integration admission route, so the real Gmail request reached
the host successfully and received:

```text
HTTP 404 Not Found
{"detail":"Not Found"}
```

This proved the failure was not caused by VLAN/L2/L3 reachability, Gmail OAuth,
the persisted integration bearer, tenant binding, audience or installation
identity. The request was delivered to the wrong application.

The repository contract already distinguishes the listeners:

- `attention_router.web.ingress_app:app` — Neutral Integration Ingress,
  default port `18101`;
- `attention_router.web.internal_ingress_app:app` — Internal Ingress,
  default port `18102`.

After the Neutral Integration Ingress was deployed separately and the legacy
scheduler was pointed to that surface, real Gmail requests were admitted with
HTTP `202 Accepted`.

### 2. Channel Sync retained its own stale host-private runtime env

The initial Channel Sync cutover still reported
`GMAIL_PRODUCT_INGRESS_FAILED` even after the legacy scheduler had been fixed.

Runtime inspection showed that Channel Sync did not read the legacy scheduler env
directly. Its private Compose interpolation pointed at a dedicated Channel Sync
runtime env, and that file still contained the old Internal Ingress target.

The dedicated Channel Sync env was corrected to the Neutral Integration Ingress
target and the container was force-recreated so the new environment was actually
loaded.

After recreation, requests originated from the dedicated Channel Sync trust zone,
were accepted by the Neutral Integration Ingress, and the Channel Sync cycle
completed with `failed=0`.

## Live evidence

The proof established all of the following without exposing provider or
integration secrets:

- Neutral Integration Ingress health returned HTTP `200`;
- a synthetic invalid credential reached the Neutral Integration Ingress and
  returned the expected governed `401 UNAUTHENTICATED` contract, proving the
  correct application surface before any live Gmail cutover;
- the legacy scheduler then produced real HTTP `202 Accepted` admissions;
- Channel Sync configuration check returned `google.gmail enabled=True`;
- the legacy scheduler was stopped before Channel Sync was started;
- the recreated Channel Sync container used the corrected Neutral Integration
  Ingress target;
- the Neutral Integration Ingress observed live requests from the dedicated
  Channel Sync trust zone and returned HTTP `202 Accepted`;
- the Gmail history cursor advanced naturally on the successful Channel Sync
  cycle;
- no `gmail_history_id` reseed was performed;
- admitted work remained pending because integration dispatch intentionally
  remains disabled.

## Security and authority properties preserved

The cutover did not alter:

- ProviderAuthorization identity or status;
- integration binding identity;
- persisted integration credential/bearer material;
- Gmail OAuth scopes or refresh grants;
- Gmail cursor semantics;
- PostgreSQL schema or migrations;
- WhatsApp runtime;
- context, memory or persistence enablement;
- attachment ingestion.

No provider token, integration bearer, Gmail body or private response payload was
printed or committed.

The Neutral Integration Ingress still revalidates the persisted credential,
binding, audience and tenant authority on every admission. Network reachability
does not grant application authority.

## Network and deployment boundary

The VLAN and routed connectivity investigation was already complete before this
application diagnosis. No further MikroTik or VLAN mutation was required for the
successful cutover.

The Neutral Integration Ingress now has its own host-private deployment identity
rather than sharing the Internal Ingress application surface. Exact private
addresses and host inventory are intentionally not recorded in Git.

IPAM remains deferred by operator decision. The address used for this live proof
was allocated manually under explicit operator authorization after collision
checks. This is an operational exception, not a new allocation policy. A future
IPAM bootstrap must import the existing allocation before IPAM becomes
authoritative for subsequent assignments.

## Host-private configuration follow-up

The live Neutral Integration Ingress currently reuses an existing protected env
file because the remote-execution security boundary refused to copy secret values
into a new file automatically. Runtime feature overrides explicitly disable
unrelated Gmail, attachment, Artifact, memory/context and cognitive features for
that container.

This is operationally functional but should be hardened later by creating a
dedicated minimal Neutral Integration Ingress env through an operator-controlled
secret-management path. No secret values belong in Git.

## Current live state

At the end of this checkpoint:

- Channel Sync V1 is active and healthy;
- Neutral Integration Ingress is active and healthy;
- legacy Gmail scheduler is stopped;
- Gmail admission is succeeding;
- Gmail cursor progression is natural;
- integration dispatch remains disabled;
- WhatsApp remains untouched and operational;
- context/memory/persistence enablement remains a separate future step.

## Rollback

If Channel Sync must be rolled back:

1. stop Channel Sync;
2. confirm it is no longer scheduling;
3. restore the legacy scheduler with the correct Neutral Integration Ingress
   target;
4. preserve ProviderAuthorization, binding, credential and Gmail history cursor;
5. never reseed the cursor as part of container rollback.

## Follow-up boundary

The live Channel Sync deployment and Gmail admission path are proven.

The next work must remain separate:

1. harden the Neutral Integration Ingress host-private env;
2. reconcile the manual private allocation into the future IPAM bootstrap;
3. decide and validate Integration Dispatch enablement;
4. only after channel operation is stable, address context, storage, memory and
   persistence enablement.

No historical backfill, attachment enablement or context/memory enablement is
part of this checkpoint.
