# Owner Identity + Sensitive Disclosure V1

Status: design source of truth for implementation on `feat/owner-identity-sensitive-approval`.

## Governance

GitHub is the source of truth and MUST stay ahead of AGT. Implementation, tests, migration changes and runtime contracts are created and committed here first. AGT may only consume an exact GitHub SHA for validation/deployment. Local AGT worktrees are never authoritative development state.

## Owner identity

Andy must resolve two distinct identities in every conversation:

1. the interaction actor: the person currently talking to Andy;
2. the represented subject: the OWNER whose account Andy represents.

The represented subject is resolved from authenticated Human Identity + active OWNER tenant membership. Public naming is not hardcoded in prompts or code.

A mutable owner profile exposes:

- `assistant_reference_name`: how Andy should refer to the OWNER externally.

The Android app will later expose this as a configurable field. Until the UI is changed, the backend contract may be provisioned independently. If no public reference name exists, Andy must use a neutral fallback such as "titular da conta"; it must never guess a name.

Planned client API:

- `GET /api/v1/client/profile`
- `PATCH /api/v1/client/profile`

Only an authenticated OWNER session for the active tenant may change the public reference name.

## Assistant provenance

Every automated WhatsApp text response must carry deterministic assistant provenance, independent of model behavior:

`[Andy] ...`

The first automated response in a conversation context must also identify Andy explicitly as the virtual assistant of the represented OWNER. Later responses must not repeat the full introduction unnecessarily.

Conversation introduction state must be durable and based on delivered Andy output, not merely on a model decision.

Andy must never write as if she were the represented OWNER.

## Sensitive location disclosure

A third party asking for the represented OWNER's current location does not obtain location merely because `location.current` is operational.

Required flow:

`request -> requester identity/relationship -> disclosure authorization -> Android approval -> location.current -> reply to original conversation`

Before asking the OWNER for authorization, the Router should use already-bound requester facts and ask only for missing identity/relationship fields.

The disclosure request must preserve:

- tenant;
- original interaction/event;
- requester binding/contact;
- requester display name and relationship;
- represented OWNER identity;
- canonical capability;
- original WhatsApp recipient;
- immutable authorization/fingerprint lineage;
- expiration and terminal result.

No location provider read may happen before a valid human approval.

## Android approval inbox

Reuse Native App Approval V1A:

- `GET /api/v1/client/approvals/pending`
- `GET /api/v1/client/approvals/{approval_id}`
- `POST /api/v1/client/approvals/{approval_id}/decision`

The approval channel for this flow is `android_client`.

Example preview:

> Sr. Francisco Escossio solicitou acesso à sua localização atual. Relação informada: pai. Aprovar permite uma consulta única à localização atual e o retorno da informação nesta conversa.

Current Android already renders pending approvals with Approve / Deny. Push notification is outside this backend increment; foreground/refresh inbox behavior remains valid.

## Continuation contract

APPROVE:
1. revalidate Human Identity, tenant, frozen fingerprint and expiry;
2. execute exactly one `location.current` read for the represented OWNER;
3. build exactly one reply for the original requester;
4. deliver via the original conversation;
5. mark the authorization consumed and the disclosure request terminal only after delivery.

DENY:
- never read the location provider;
- return a bounded denial to the original conversation.

EXPIRED / invalid / unavailable:
- fail closed;
- do not disclose location;
- return a bounded status message when appropriate.

## Hardcoded identity removal

Production code, prompt instructions and behavior profiles must not contain a fixed human name such as "Alex". Semantic identifiers must use OWNER/represented-subject concepts instead of a person's name.

## Deployment rule

No live deployment is part of this design commit.

After implementation is committed on GitHub and CI is green, AGT may:
1. fetch the exact branch SHA;
2. run PostgreSQL/migration validation;
3. validate runtime against that SHA;
4. deploy only with explicit approval.
