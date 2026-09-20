# User Idiolect V1 — Storage & Retrieval Contract

Status: active runtime implementation — V1D/V1E/V1F/V1G implemented; V1H isolation and directionality verification

Related:
- #84 Personal Context V1
- #89 Intent Clarification V1
- #90 User Idiolect V1
- docs/architecture/pending-intent-v1-contract.md

Implementation checkpoint (2026-09-20):
- V1D: resolved clarification -> bounded confirmed language FactRow;
- V1E: tenant/person/conversation-scoped deterministic retrieval;
- V1F: confirmed meaning may resolve only an already-ambiguous closed candidate set;
- V1G: explicit correction records supersession and removes superseded facts from active retrieval;
- V1H: proves tenant/person isolation plus USER_TO_ANDY vs ANDY_TO_USER/reuse-policy directionality.

## 1. Goal

Represent how a specific person tends to encode meaning and communicate, while reusing the existing Personal Context primitives.

Do not create a parallel ungoverned personality/profile database.

The storage model deliberately separates:

- explicitly confirmed/user-declared language knowledge;
- observed/inferred language patterns;
- interpretation assistance;
- Andy response-style accommodation.

Knowledge is not authority.

## 2. Existing primitives to reuse

Current Personal Context already composes:

- `FactRow`
- `MemoryClaimRow`

and resolves the represented owner by tenant-scoped canonical actor identity.

This is sufficient for V1 when the evidence classes are mapped intentionally.

## 3. Evidence-class mapping

### 3.1 Explicitly confirmed or user-declared language knowledge -> FactRow

Use `FactRow` when the user explicitly confirms a meaning or explicitly declares a communication preference.

Examples:

- user confirms that `essa parada` referred to one specific currently discussed feature;
- user says `quando eu disser deixa quieto, quero dizer cancelar`;
- user explicitly asks Andy to be shorter/more technical/more informal.

Recommended semantics:

- `subject_type = ACTOR`
- `subject_id = canonical owner actor key`
- `source_type = INTENT_CLARIFICATION` or `USER_DECLARATION`
- `source_ref = pending_intent_id` or canonical declaration evidence id
- `fact_class = USER_CONFIRMED_LANGUAGE` / `USER_DECLARED_COMMUNICATION_PREFERENCE`
- bounded `valid_from/valid_until`
- `supersedes_fact_id` for explicit correction
- metadata preserving contextual scope and direction

An explicit confirmation is strong evidence about the resolved context. It is not proof that the same expression has one timeless global meaning.

### 3.2 Observed or inferred language patterns -> MemoryClaimRow

Use `MemoryClaimRow` for patterns inferred from one or more conversations.

Examples:

- repeated use of a nickname;
- apparent preference for terse technical answers;
- recurring pragmatic use of `deixa quieto`;
- repeated tolerance for informal/slang register.

These remain claims, not facts.

Existing claim features are intentionally useful here:

- confidence;
- source_quality;
- staleness_class;
- valid_from/valid_until;
- supersedes_claim_id;
- conflict_group_id;
- first/last observed;
- MemoryEvidence provenance.

## 4. Predicate namespaces

V1 should use explicit namespaces rather than ad-hoc free-form keys.

Suggested initial namespaces:

### Meaning / understanding
- `idiolect.lexical_mapping`
- `idiolect.pragmatic_mapping`
- `idiolect.reference_alias`

### User communication preference
- `communication.preference.directness`
- `communication.preference.formality`
- `communication.preference.technical_depth`
- `communication.preference.response_length`

### Observed communication style
- `communication.observed.slang`
- `communication.observed.profanity_tolerance`
- `communication.observed.humor`
- `communication.observed.affectionate_register`

The namespace is semantic and provider-independent.

## 5. Structured value contract

A language item should carry structured values rather than relying only on free text.

For lexical/pragmatic mappings, `value_json/object_json` should preserve semantics equivalent to:

- `expression`
- `normalized_expression`
- `meaning_kind`
- `semantic_intent_key` or `referent_key` when applicable
- `parameters` when bounded/validated
- `context_scope`
- `direction`
- `evidence_class`
- `reuse_policy`
- `generalization_scope`

### Context scope examples

- one conversation/thread;
- one capability/feature;
- one domain;
- one channel;
- global-for-user only when explicitly justified.

Default generalization should be narrow.

## 6. Directionality

Every item that can influence output style must distinguish at least:

- `USER_TO_ANDY_LANGUAGE`
- `ANDY_TO_USER_PREFERENCE`

Examples:

User:
`meu anjo lindo, desative por favor`

This may create evidence that:
- affectionate vocative is used USER_TO_ANDY;
- politeness/warmth is observed.

It does not create:
- permission for Andy to call the user `meu anjo lindo`.

Direction is part of the semantic value, not an informal convention.

## 7. Reuse policy

Observation and imitation must be separate.

Suggested reuse modes:

- `INTERPRET_ONLY` — may help understand the user; never reuse phrase toward user.
- `STYLE_SIGNAL` — may influence abstract style dimensions, not copy the phrase.
- `CONTEXTUAL_REUSE` — phrase/style may be reused only in matching contexts.
- `EXPLICIT_REUSE` — user explicitly requested/preferred this form.

Passive observation must never automatically become `EXPLICIT_REUSE`.

Profanity, insults, intimate language, sensitive labels, or highly personal vocatives should default to `INTERPRET_ONLY` or at most safe aggregate style signals unless the user explicitly establishes an outbound preference.

## 8. Confidence has two meanings

Do not conflate:

1. **evidence confidence** — how certain we are that the user meant X in the observed event;
2. **generalization confidence** — how safe it is to reuse that mapping outside the original context.

Example:

The user explicitly confirms:
`essa parada` = feature X.

Evidence confidence for that event may be effectively certain.

Generalization confidence to all future conversations should still be low/narrow until repeated or explicitly generalized by the user.

Store enough structure to preserve that distinction.

## 9. Pending Intent -> Fact admission

A successfully RESOLVED Pending Intent from #89 can emit one explicit language fact when the resolution carries reusable semantic information.

Source chain:

`FactRow.source_ref -> PendingIntent -> original inbound event + resolution inbound event`

This avoids forcing owner-control messages into the conversation-memory evidence schema merely to preserve provenance.

The Pending Intent remains operational evidence; FactRow is the Personal Context projection.

Admission rules:

- only RESOLVED;
- exact tenant/person match;
- no admission from EXPIRED/CANCELED/SUPERSEDED;
- selected candidate/referent must be frozen and validated;
- context scope must be explicit;
- default reuse policy is conservative;
- no secrets/credentials;
- sensitivity classification still applies.

## 10. Observed pattern admission

Passive observations continue through the existing archive/extraction path:

`conversation_messages -> extraction run -> memory candidate -> MemoryClaimRow + MemoryEvidence`

The idiolect extractor must be bounded and structured.

One occurrence may create at most a low-confidence observation/candidate, not a stable behavior mutation.

Promotion thresholds should depend on:
- recurrence;
- source quality;
- consistency;
- contradiction rate;
- freshness;
- whether the user explicitly confirmed or corrected the pattern.

## 11. Retrieval

Do not feed raw conversation history to the interpreter merely to mimic the user.

Introduce a bounded idiolect retrieval view over Personal Context.

Suggested caller contract:

- tenant/person identity;
- current utterance;
- conversation/domain/capability context;
- limit.

Return only relevant language items with:

- predicate;
- structured value;
- evidence/generalization confidence;
- direction;
- reuse policy;
- freshness;
- provenance class.

### Initial ranking order

Prefer:

1. explicit current user instruction;
2. exact clarification-confirmed mapping in matching context;
3. repeated high-confidence observed mapping in matching context;
4. inferred pattern;
5. broader style signal.

Exact/narrow context beats global similarity.

## 12. Retrieval implementation

V1 can stay deterministic.

For lexical mappings:
- normalized phrase/n-gram matching;
- exact alias matching;
- context compatibility;
- confidence/freshness ranking.

For communication style:
- retrieve structured preference predicates rather than search raw user text.

This may be implemented as a specialized bounded projection over the existing Personal Context snapshot. It does not require a new storage database.

Semantic/vector retrieval can be introduced later behind the same contract.

## 13. Interpretation use

Retrieved idiolect items are **evidence**, not forced parse results.

The semantic interpreter may use them to raise/lower candidate support.

Rules:

- explicit matching fact may strongly support one candidate;
- inferred/observed claim may only bias interpretation;
- if effectful alternatives remain materially plausible, #89 still clarifies;
- no idiolect item can create an action outside the closed semantic registry;
- no idiolect item bypasses capability or authority validation.

## 14. Style accommodation

Output adaptation should happen after meaning/authority decisions, not inside them.

Use structured dimensions rather than raw mimicry where possible.

Example aggregate profile:

- directness: high
- technical_depth: high
- formality: low
- response_length: medium
- humor: occasional

Andy may adapt these dimensions within product/safety/personality bounds.

Do not mechanically reproduce:
- every profanity;
- every typo;
- intimate vocatives;
- insults;
- discriminatory language;
- manipulative patterns;
- personally sensitive labels.

The goal is accommodation, not impersonation.

## 15. Contradiction and correction

Explicit correction outranks inference.

Examples:

- user says `não me chama mais assim` -> outbound preference is superseded immediately;
- user clarifies that an alias now refers to another device -> create new scoped fact and supersede/conflict with the previous one;
- observed style changes over time -> older MemoryClaim may decay/stale rather than be erased.

Keep history; change active interpretation.

## 16. Expiry / scope

Language is context-sensitive.

Default policy should avoid permanent global mappings unless:
- user explicitly generalizes them; or
- repeated stable evidence supports a broader scope.

Potential expiry classes:

- conversation-local;
- short-lived contextual;
- domain-stable;
- user-stable explicit preference.

A contextual alias should not silently live forever.

## 17. Privacy

Idiolect can itself be personal data.

Requirements:

- tenant/person isolation;
- bounded retrieval;
- sensitivity classification;
- no secret capture;
- no cross-tenant aggregate learning from private expressions;
- no raw-history dump to downstream behavior;
- auditable provenance;
- user correction/removal path in future Personal Context UI.

## 18. V1 tests

### Storage
- clarification-confirmed mapping creates one FactRow projection;
- duplicate resolution does not duplicate fact;
- explicit correction supersedes prior fact;
- canceled/expired Pending Intent creates no confirmed fact;
- inferred pattern remains MemoryClaim, not FactRow.

### Isolation
- same phrase can mean different things for two tenants;
- same phrase can mean different things for two people if multi-person tenant support applies;
- no cross-tenant retrieval.

### Directionality
- USER_TO_ANDY vocative does not become ANDY_TO_USER preference;
- observed profanity does not become phrase reuse permission;
- explicit outbound preference can permit bounded reuse.

### Retrieval
- exact contextual mapping outranks broad inferred pattern;
- expired mapping is excluded;
- conflicting mapping causes lower confidence/fail-closed behavior;
- no lexical evidence returns no idiolect item.

### Clarification interaction
- strong idiolect evidence may avoid unnecessary clarification only when one meaning is materially established;
- effectful ambiguity still clarifies when alternatives remain plausible;
- idiolect never creates execution authority.

## 19. Example lifecycle

First encounter:

User:
`essa parada aí deixa desativada`

Andy cannot resolve the referent safely.

#89 creates Pending Intent and asks which feature is meant.

User resolves:
`estou falando das respostas automáticas`

Result:

1. Pending Intent RESOLVED;
2. selected semantic referent/action validated;
3. authority checked independently;
4. optional execution occurs;
5. FactRow projection records a narrow confirmed language mapping:
   - expression: `essa parada`
   - referent: automatic-response feature
   - context: current domain/conversation
   - direction: USER_TO_ANDY_LANGUAGE
   - reuse: INTERPRET_ONLY
   - source: Pending Intent.

Later:

User:
`essa parada tá rápida demais`

Idiolect retrieval finds the matching contextual fact and helps interpretation.

If the context still admits two materially different effectful meanings, Andy asks again.

That is learning without hallucinating certainty.
