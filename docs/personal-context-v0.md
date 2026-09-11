# Personal Context V0

Personal Context is the tenant-scoped read model for the person Andy represents.

It answers a narrow foundational question: **what stable, attributable knowledge does this tenant currently hold about its represented owner?**

## Boundary

V0 composes existing canonical primitives instead of creating a parallel memory database:

- `ActorBindingRow` resolves the represented owner inside the tenant;
- multiple provider/source bindings are allowed when they converge on one canonical owner actor key;
- `MemoryActorRow` aliases bridge provider identities to existing persistent memory;
- active `MemoryClaimRow` entries provide learned/self-reported claims;
- canonical `FactRow` entries provide structured owner facts;
- every lookup is tenant-scoped;
- expired, inactive and `SECRET` memory claims are excluded fail-closed;
- reads are bounded.

## Knowledge is not authority

Personal Context does **not** grant permission to disclose a fact or perform an action.

Knowing `payment.pix.primary`, an address, a relationship or a preference is separate from deciding whether that information may be used in a response. Disclosure policy/authority remains a separate future boundary.

This V0 is therefore read-only and is not automatically injected into outbound model prompts.

## Provenance

The read model preserves the provenance already carried by the underlying stores:

- memory: `source_quality`, confidence, observation times, validity and staleness;
- facts: `source_type`, `source_ref`, confidence, observation time and validity.

The original evidence remains canonical in the existing memory/archive system; Personal Context does not duplicate it.

## Owner resolution

The tenant may have several source identities for the same owner (for example WhatsApp and e-mail). V0 accepts that only when all active owner bindings resolve to the same canonical actor key.

No owner or more than one canonical owner fails closed with `REPRESENTED_OWNER_NOT_UNIQUE`.

## Non-goals

V0 deliberately does not add:

- embeddings or semantic retrieval;
- machine learning;
- automatic personality mutation;
- disclosure grants;
- application UI;
- owner write/edit APIs;
- prompt injection into the decision engine;
- PostgreSQL RLS.

Those layers can build on this stable owner-scoped read model without changing its ownership semantics.
