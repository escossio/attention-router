# Personal Language & Intent Resolution V1

Status: architectural frontier / pre-implementation design

Related issues:
- #84 — Personal Context V1
- #88 — Semantic owner-control E2E checkpoint
- #89 — Intent Clarification V1
- #90 — User Idiolect V1

## Purpose

This document defines two complementary intelligence boundaries for Andy:

1. **Intent Clarification** — detect when natural-language meaning is materially ambiguous and resolve that ambiguity with the human before effectful execution.
2. **User Idiolect** — learn, with provenance and user scope, how a particular person tends to encode meaning and which aspects of that person's communication style Andy may safely accommodate.

The two boundaries form a learning loop but must remain separate because they answer different questions:

- Intent Clarification: **do we understand this request well enough to act?**
- User Idiolect: **what has this person historically meant by expressions like this, and how do they prefer to communicate?**

Neither boundary grants execution or disclosure authority.

## Motivating examples

### Ambiguous operational request

User:

`retorne em 30 segundos`

Materially plausible interpretations include:

- reply to the current conversation after 30 seconds;
- resume a paused operation after 30 seconds;
- set the owner's default reply-grace configuration to 30 seconds.

A model choosing one of these interpretations with high confidence is not sufficient if the alternatives produce materially different effects.

The correct behavior is to resolve meaning before authority is exercised.

### Contextual reference

User:

`essa parada aí deixa desativada`

The phrase `essa parada` may refer to the currently discussed feature, Andy herself, an automation mode, a transport, or another recent object.

If the referent changes the effect, Andy must clarify.

After the user explicitly resolves the reference, that clarification becomes high-quality linguistic evidence about how that person uses the phrase in that context.

### Style and directionality

User:

`meu anjo lindo, desative e não responda mais ninguém, por gentileza`

This gives evidence that the user may address Andy with warm, affectionate language. It does **not** prove that Andy should address the user using the same vocative.

The architecture must distinguish language observed from the user from language Andy is authorized or expected to reuse toward the user.

---

## 1. Separation of concerns

### 1.1 Intent interpretation

Produces candidate semantic/canonical interpretations from natural language.

This may use deterministic parsing, structured retrieval, or generative AI.

Interpretation is not execution authority.

### 1.2 Ambiguity evaluation

Determines whether competing interpretations are materially different enough that silent selection would be unsafe or misleading.

This evaluation must consider both:

- semantic ambiguity;
- consequence/effect difference.

Confidence alone is not sufficient.

### 1.3 Clarification resolution

Creates and resolves a durable pending question when meaning is insufficiently established.

The human answer resolves semantics only.

It does not manufacture missing capability grants, permissions, policies, or disclosure authority.

### 1.4 Idiolect learning

Stores bounded evidence about how one person uses language.

The idiolect layer may improve future interpretation and communication style, but it remains contextual knowledge.

### 1.5 Execution/disclosure authority

Remains in existing deterministic policy/capability/security boundaries.

No learned phrase, inferred preference, or clarification response bypasses those boundaries.

---

## 2. Intent Clarification V1

### 2.1 Target flow

`inbound language -> candidate interpretations -> ambiguity/effect evaluation -> clarification pending -> human resolution -> canonical intent -> deterministic validation -> authority -> execution`

### 2.2 Pending Intent

Introduce a durable semantic object equivalent to a `PendingIntent`.

It represents unresolved meaning, not a queued executable action.

Minimum semantics:

- tenant scope;
- canonical person/owner scope;
- original utterance evidence reference;
- conversation/context reference;
- candidate canonical intents;
- material-difference metadata;
- confidence/evidence per candidate;
- clarification prompt/reference;
- created/expiry timestamps;
- correlation rules for the resolving reply;
- selected candidate only after successful resolution;
- provenance of the resolution.

Suggested states:

- `PENDING`
- `RESOLVED`
- `REJECTED`
- `CANCELED`
- `EXPIRED`

### 2.3 Correlation invariant

Short answers such as:

- `sim`
- `não`
- `a segunda`
- `isso`
- `é isso mesmo`

have no independent semantic authority.

They may resolve a Pending Intent only when the system can unambiguously correlate them with the exact clarification question and candidate set.

A stale, cross-person, cross-tenant, or multiply plausible confirmation fails closed.

### 2.4 Clarification policy

Andy must not become bureaucratic.

The first policy model should distinguish at least:

#### Unambiguous
One canonical interpretation is sufficiently established and alternatives are not materially different.

Proceed normally.

#### Low-impact ambiguity
Multiple readings exist, but choosing the most contextually supported one has negligible effect.

Policy may allow bounded interpretation or a lightweight clarification.

#### Effectful ambiguity
Interpretations differ in configuration, external communication, disclosure, money, security, device control, deletion, scheduling, capability use, or another meaningful side effect.

Clarification is mandatory before execution.

### 2.5 Candidate constraints

Generative AI may propose candidate meanings, but candidates must map into closed, registered semantic/action schemas.

The model may not invent arbitrary tools, shell commands, permissions, or capabilities as clarification choices.

### 2.6 Confirmation is not authorization

The human may confirm:

`sim, quero que desative`

That confirms meaning.

A later authority boundary must still decide whether that actor may disable the feature and whether additional approval is required.

---

## 3. User Idiolect V1

### 3.1 Definition

The idiolect layer represents how a specific person tends to encode meaning and communicate.

It is broader than a keyword dictionary.

It should capture at least four semantic classes.

### 3.2 Personal lexicon

Expressions, nicknames, aliases, references, and colloquialisms associated with contextual meaning.

Examples:

- `essa parada`
- `o bichinho`
- `essa porra`
- a personal nickname used for Andy;
- a household nickname for a device;
- a recurring shorthand for a routine.

A lexical item is never automatically universal or timeless.

### 3.3 Pragmatic intent patterns

Patterns in which a phrase commonly maps to a communicative act or intent.

Example:

`deixa quieto`

may mean cancel the current operation in one user's conversational context.

The stored knowledge should describe the context in which the mapping has been observed or confirmed.

### 3.4 Communication style

Observed/preferred dimensions may include:

- directness;
- formality;
- technical depth;
- message length;
- slang;
- profanity tolerance;
- humor;
- affectionate language;
- verbosity;
- recurring discourse markers;
- preferred explanation style.

Style adaptation must be gradual and governed.

### 3.5 Accommodation policy

Observation is not permission to imitate.

The system must distinguish what was observed from what Andy is allowed or expected to reuse.

At minimum preserve directionality:

- `USER_TO_ANDY_LANGUAGE`
- `ANDY_TO_USER_PREFERENCE`

A phrase used by the user toward Andy does not automatically become a phrase Andy should use toward the user.

### 3.6 Evidence model

An idiolect evidence item should preserve semantics equivalent to:

- tenant;
- canonical person;
- expression/pattern;
- semantic or pragmatic interpretation;
- contextual qualifiers;
- direction;
- source evidence references;
- evidence class;
- confidence;
- recurrence count;
- first observed;
- last observed;
- freshness/expiry;
- contradiction/supersession state;
- reuse/accommodation permission.

Suggested evidence classes:

- `OBSERVED`
- `EXPLICITLY_CONFIRMED`
- `USER_DECLARED`
- `INFERRED`

The ranking of evidentiary strength should prefer:

1. explicit user instruction/preference;
2. clarification-confirmed meaning;
3. repeated high-confidence observations;
4. inferred patterns.

### 3.7 One observation is not a rule

One occurrence may create a low-confidence observation.

It must not silently establish a permanent lexical mapping or mutate Andy's default response style.

Repeated evidence, explicit confirmation, and contextual consistency increase confidence.

### 3.8 Correction and contradiction

Explicit user correction wins over inferred patterns.

The model must support:

- supersession;
- contradiction;
- decay;
- expiry;
- contextual narrowing.

Example:

A phrase that once referred to one feature may later be used differently in another context.

The architecture must preserve both the historical evidence and the scoped current interpretation rather than flattening all occurrences into one global definition.

---

## 4. The combined learning loop

The two boundaries reinforce one another:

`user utterance`
-> `interpretation`
-> `ambiguity detected`
-> `clarification question`
-> `human resolution`
-> `canonical intent/referent`
-> `deterministic authority/execution boundary`
-> `clarification evidence emitted`
-> `idiolect evidence admitted into governed Personal Context`
-> `future retrieval improves interpretation`

The key architectural insight is:

**Intent Clarification becomes a supervised evidence generator for User Idiolect.**

This is stronger than passive observation because the human explicitly resolved the meaning.

---

## 5. Relationship to Personal Context V1

#84 remains the parent contextualization frontier.

Do not create an ungoverned parallel profile database.

Idiolect evidence should build on the same principles already established for Personal Context:

- tenant isolation;
- canonical owner/person resolution;
- provenance;
- confidence;
- freshness;
- sensitivity;
- contradiction/supersession;
- bounded retrieval;
- knowledge separated from authority.

The idiolect layer is a specialized semantic class inside the user's contextualization space.

Intent Clarification, however, is also an execution-safety boundary and therefore must remain logically separable from storage/retrieval.

---

## 6. Retrieval contract

Consumers must not receive raw conversation history merely to imitate the user.

Instead, expose a bounded language-context contract containing only relevant admitted items.

A consumer may receive, for example:

- known contextual aliases relevant to current entities;
- confirmed pragmatic mappings;
- current style preferences;
- accommodation permissions;
- provenance/confidence.

The retrieval boundary must remain tenant/person-scoped and explainable.

---

## 7. Safety and privacy invariants

- tenant isolation is mandatory;
- person identity must be canonical, not inferred from an untrusted transport identifier alone;
- idiolect is knowledge, not execution authority;
- idiolect is knowledge, not disclosure authority;
- clarification resolves meaning, not permission;
- effectful ambiguity fails closed;
- cross-tenant language learning is forbidden;
- raw secrets/credentials are never learned as style or lexicon;
- sensitive language/context follows Personal Context sensitivity policy;
- explicit correction outranks inference;
- stale mappings may decay or expire;
- Andy must not mechanically mimic abusive, hateful, sexual, manipulative, or otherwise inappropriate language merely because it was observed;
- generated clarification options remain inside registered semantic/capability schemas.

---

## 8. Non-goals for V1

Do not:

- fine-tune a per-user foundation model;
- create unrestricted long-term transcript replay;
- build a global slang dictionary from private tenants;
- automatically imitate all observed vocabulary;
- let language confidence bypass confirmation policy;
- grant actions based on learned phrases;
- create hidden personality mutations;
- auto-merge linguistic inferences into immutable facts.

---

## 9. Initial proof plan

### Intent Clarification proof

1. Feed one utterance with two materially distinct canonical interpretations.
2. Prove it enters `CLARIFICATION_PENDING`.
3. Prove an uncorrelated `sim` cannot resolve it.
4. Prove a correctly correlated answer resolves exactly one candidate.
5. Prove expiry/cancel paths.
6. Prove the resolved meaning still passes through normal capability/authority validation.
7. Emit a provenance-bearing clarification evidence item.

### User Idiolect proof

1. Create two tenants/persons using the same phrase with different confirmed meanings.
2. Prove complete isolation.
3. Admit one clarification-confirmed lexical mapping.
4. Retrieve it only in the matching context.
5. Prove a single low-confidence observation does not alter response style.
6. Prove USER_TO_ANDY language does not become ANDY_TO_USER preference automatically.
7. Prove explicit correction supersedes inference.
8. Prove expired/stale contextual mappings fail closed or lose ranking priority.

### Integrated proof

Use:

`essa parada aí deixa desativada`

Expected sequence:

1. referent ambiguity detected;
2. Andy asks a bounded clarification;
3. user confirms the intended feature;
4. canonical action is validated independently;
5. clarification produces an `EXPLICITLY_CONFIRMED` idiolect evidence item;
6. later use of `essa parada` in the same context benefits from that evidence;
7. a materially different effect still triggers clarification when necessary.

---

## 10. Recommended implementation order

1. Freeze #89 Pending Intent contract and state machine.
2. Prove correlation/expiry/authority invariants synthetically.
3. Define the idiolect evidence schema as an extension of #84, not a parallel store.
4. Emit one clarification-resolution evidence item.
5. Add bounded idiolect retrieval.
6. Add interpretation assistance from retrieved idiolect evidence.
7. Only then introduce controlled style accommodation.
8. Keep high-impact ambiguity clarification mandatory until evidence and policy prove a safer relaxation.

---

## 11. Relationship to checkpoint #88

#88 remains narrowly about outbound confirmation delivery becoming `AMBIGUOUS` after the semantic owner command has already succeeded.

The phrase used during that proof exposed the need for #89, but the new intelligence architecture must not blur the current defect.

Finish #88 on its own transport/ACK boundary.

The new clarification/idiolect work begins as a separate frontier after its contracts are reviewed.
