# Owner Response Review Channel V0

## Purpose

Give the represented owner a narrow WhatsApp self-chat channel to approve or reject an existing pending Andy response review without adding a second review state machine.

## Existing authority reused

V0 reuses the existing authenticated WWEBJS owner-control boundary. A text command has no authority by itself. The inbound event must already be classified as an authenticated owner self-chat command and the active actor binding must resolve to the represented tenant owner.

The response state machine remains `AgentResponseReviewRow`:

- `PENDING` → `APPROVED`
- `PENDING` → `REJECTED`

Approval creates or reuses the existing `AgentExecutionIntentRow` with `authorization_source=HUMAN_REVIEW`. The same authenticated owner command then asks the existing execution layer to release that intent. No release bypass exists: `release_intent()` still checks the normal execution gate, recipient resolution, global external-delivery gate and transport readiness.

If those gates pass, the human-reviewed intent becomes `READY` / `RELEASED` and the normal worker may enqueue the response for the local transport. If any gate blocks, the review remains `APPROVED` but the intent is not made ready; the owner confirmation states that delivery was not released.

## Owner request

When a response review is created, the application attempts to enqueue one idempotent text message through the already-proven `owner_control_text` / `local_transport` route.

The request contains:

- the proposed response, bounded to a short preview;
- a 12-character display reference derived from the review UUID;
- `aprovar resposta <referência>`;
- `negar resposta <referência>`.

No new database table or migration is introduced. If the represented owner or WWEBJS binding is unavailable or ambiguous, the request is not sent and the block is audited.

## Command resolution

The reference is not trusted as a database identity. The server resolves it inside the current tenant and requires exactly one matching review. Zero matches or multiple matches fail closed.

Questions are not commands. Malformed approval-like text is consumed and rejected rather than falling through as a normal conversation command.

## Replay and terminal state

Repeating the same decision is idempotent at the review layer:

- approve after approve reuses the existing execution intent;
- if a previous release was blocked, a repeated authenticated approval may re-evaluate the normal release gates;
- reject after reject remains rejected.

Trying the opposite terminal decision remains a conflict and does not rewrite history.

## Explicitly out of scope

V0 does not:

- add Meta interactive buttons;
- add quoted-message correlation to WWEBJS;
- create a second human-approval table;
- change the Andy decision engine;
- change policies;
- bypass execution or external-delivery safety gates;
- automatically merge or deploy anything.

Quoted-message binding can be added later if the local transport exposes a stable quoted-message identifier. It is not required for this V0 because the command source is already an authenticated owner self-chat and the review reference is resolved fail-closed.
