# WhatsApp group contextual presence

This feature is exclusively for one owner-authorized group. The exact `@g.us` ID and tenant ID are private deployment inputs, never repository data. The default is disabled. Existing direct-conversation and manual-reply group denials remain unchanged.

## Communication and trust boundaries

| Source | Destination | Direction | Authority | Data |
| --- | --- | --- | --- | --- |
| Existing WhatsApp Browser | Existing Transport | Existing CDP attachment | Existing authenticated session | New group messages |
| Existing Transport | Internal ingress | Existing signed bridge | Existing ingress HMAC and tenant | Message, group ID, participant ID, quote reference |
| Existing worker | Existing database | Existing connection | Private exact group and tenant allowlist | Bounded context, mode, proposal outcome, outbox |
| Existing worker outbox | Existing Transport | Existing signed send path | Mode, notice, exact group, frequency, identity and HMAC | Labelled text only |

No new WhatsApp client, session, container, network or IP is introduced. Live continuity consumes only events received after OBSERVE activation. Historical acceleration is intentionally deferred: the group history API exists, but importing older conversation content is unnecessary for this demonstration and would expand the privacy and consent scope. It has no effect on live cursors.

## Private configuration

Set these in the worker's private environment and set `GROUP_CONTEXTUAL_PRESENCE_GROUP_ID` to the same exact ID in the Transport's private environment:

```text
GROUP_CONTEXTUAL_PRESENCE_ENABLED=true
GROUP_CONTEXTUAL_PRESENCE_GROUP_ID=<confirmed exact @g.us ID>
GROUP_CONTEXTUAL_PRESENCE_TENANT_ID=<tenant receiving this account's ingress>
```

Keep `GROUP_CONTEXTUAL_PRESENCE_ENABLED=false` until the code, database migration and Transport guard are deployed and certified. The group ID is also persisted in `group_contextual_presence` by the private operator command. Do not derive an outbound destination from a title or an incoming message. Do not place the private values in Compose YAML, shell history, issue text or PR logs.

## Gradual activation

1. Certify the exact source SHA: targeted Python/Node tests, distributed PostgreSQL test, required GitHub checks and reviewed diff. Back up the current runtime configuration and confirm the existing WhatsApp session is CONNECTED. Apply migration `0058_group_contextual_presence` without changing old tables.
2. Deploy the existing worker/API release and the Transport guard from the certified SHA. Keep the group mode `OFF`. Verify Browser, Transport, Observer, ingress, worker, outbox and direct messaging health.
3. Run `python -m attention_router.application.group_contextual_control observe` inside the configured worker container. This starts the observation watermark at the command time; it does not import old messages.
4. Wait for a natural new group message. `status` must show an observed event. Check private audit counters and the `PROPOSED`/`SILENT` outcome. Outbox must contain no contextual send while mode is OBSERVE.
5. After observing a real contextual proposal, run `python -m attention_router.application.group_contextual_control notice`. Verify its outbox row is `DONE`, the Transport returned a message reference, and the owner/admin objection window is respected. `DONE` means transport acceptance; independent WhatsApp delivery acknowledgement must be checked before claiming recipient delivery.
6. Run `python -m attention_router.application.group_contextual_control contextual`. Wait for natural relevant messages. The policy may remain silent. Inspect only redacted counters and reason codes in shared logs.

`MENTION_ONLY` is available after notice by replacing `contextual` with `mention-only`. A group stop request sets mode `OFF`, including a request from an administrator; the broader stop rule honors any participant to fail safely.

## Immediate rollback

```bash
docker exec attention-router-live-flow-stage4-worker-1 \
  python -m attention_router.application.group_contextual_control off
```

Use the current worker container name after deployment. The mode change is persisted in the database and checked again immediately before an outbox dispatch. If the worker is unavailable, update this one group's row to `OFF` through the authenticated private database operator path. Do not restart Browser or clear the WhatsApp profile. A provider call already in flight may finish; pending calls are blocked.

## Evidence and limits

- `group_contextual_events` stores outcome, reason, latency and a proposal hash, without duplicating message text. The model prompt uses at most 12 group text messages from the last 30 minutes after the OBSERVE watermark. Events older than five minutes are observed but cannot trigger a reply. The existing inbound store retains source events under its established retention policy.
- Each group intervention is labelled `[Andy • IA]`. A generated proposal has no direct send capability. The outbox rechecks authorization, and the Transport rejects other groups, missing policy markers, unlabelled text and non-text group sends.
- A signed Transport `sent`/`already_sent` response confirms acceptance by the existing session and returns a provider message reference. It does not, alone, prove delivery to every group participant. Do not report that stronger claim without independent acknowledgement evidence.
