# Andy Contextual Intelligence — Architectural Design

Status: **approved design, not implemented**  
Date: 2026-09-13  
Repository: `escossio/attention-router`  
Scope: contextual intelligence, semantic interpretation, identity continuity, relationship context, behavioral pattern discovery, initiative control, and mobile/device context.

> This document records the approved architectural direction discussed on 2026-09-13. It is a design reference, not a claim that the described future layers are already implemented. Existing components are identified explicitly; proposed components remain future work until separately planned, tested, reviewed, and merged.

## 1. Executive summary

Andy must evolve from a command-oriented conversational agent into a contextual personal assistant that can understand natural language, maintain continuity across channels, correlate observations over time, infer patterns with explicit confidence, propose useful capabilities, and still remain bounded by deterministic authority and execution controls.

The central architectural rule is:

**The model may interpret, correlate, infer, rank, and propose. It does not grant itself authority to disclose, decide on behalf of the owner, or execute side effects.**

The existing Attention Router architecture already provides most of the required foundations:

- tenant-scoped actor bindings and canonical actor identity;
- recent conversation history distinct from persistent memory;
- persistent conversation archive, memory candidates, claims, evidence, provenance, confidence, staleness, and validity;
- Personal Context V0 for represented-owner knowledge;
- deterministic Context Retrieval V0 with a stable boundary that can later accept semantic retrieval;
- canonical timeline, facts, relationships, entity state, capabilities, decisions, standing directives, and authority context;
- provider-neutral Integration Contract V0;
- policy, autonomy, response review, human approval, execution intent, outbox, and delivery evidence;
- canonical event origins including device and provider events;
- Android device-identity and capability boundaries in `andy-android`.

Therefore this design does **not** introduce a second memory database, a parallel profile store, a WhatsApp-specific intelligence layer, or direct LLM execution authority. New intelligence should be added behind the existing contracts.

## 2. Problem statement

Current behavior exposes three distinct limitations.

### 2.1 Exact-command dependence

Owner controls currently rely on explicit deterministic phrases. A command such as `espera 10` is understood because the adapter contains an exact accepted pattern, while a semantically equivalent phrase such as `aguarde 10` can fail.

Teaching every synonym to a regex parser does not scale. Natural-language interpretation should translate varied phrasing into the existing typed owner-control actions without replacing their authority checks.

### 2.2 Context without sufficient situational judgment

Andy can produce linguistically plausible responses that are socially or situationally wrong. Two motivating synthetic examples are:

- receiving a third-party automatic business message and proactively offering to rewrite it even though nobody requested that intervention;
- interpreting a frustrated message as permission to cancel or postpone a social engagement on behalf of the owner.

These are not primarily language-generation failures. They expose missing boundaries between observation, inference, initiative, and authority.

### 2.3 Valuable patterns are distributed across events

A user's useful personal context may be visible only by correlating multiple sources over time: location, calendar events, messages, contacts, device observations, capabilities, and eventually financial or home-automation signals.

The architecture must support discovery such as:

- repeated visits to multiple locations on a weekly schedule;
- probable professional routines;
- recurring conversations with the same person across several channels;
- stable preferences and forms of address;
- opportunities to create schedules, maps, reminders, payment tracking, or other workflows that were not pre-modeled as a domain-specific product module.

## 3. Core design principles

### 3.1 Knowledge is not authority

Knowing a fact does not authorize disclosure or action. Personal Context already enforces this separation and it must remain fundamental.

### 3.2 Observation is not inference

Raw observations remain distinct from hypotheses. Location samples, messages, device events, and calendar entries are evidence. A probable routine or occupation is an inference with confidence and provenance.

### 3.3 Inference is not initiative

Andy may infer something useful without immediately telling the user or a third party. The system must decide whether surfacing the observation is relevant, timely, requested, permitted, and worth the interruption.

### 3.4 Initiative is not execution

Suggesting a map, schedule, reminder, or payment workflow does not authorize creation or external side effects. Existing capability, policy, review, and execution gates remain authoritative.

### 3.5 Channels are transport, not identity

WhatsApp, Telegram, SMS, email, mobile app, voice, and future channels must converge on canonical actors and normalized events. Channel-specific IDs remain provider references until resolved inside the tenant.

### 3.6 Preserve provenance

Every durable claim or hypothesis should be explainable by source evidence, observation time, extraction/correlation method, confidence, validity, and supersession history where applicable.

### 3.7 Fail closed on ambiguity

If identity, owner authority, unit, scope, target, or intended effect is ambiguous, the system clarifies or withholds action. The model does not silently choose a consequential interpretation.

## 4. Existing foundations to reuse

### 4.1 Owner Control

Current typed actions already include controls such as:

- `SET_AUTOMATIC_RESPONSES_ENABLED`;
- `SET_OWNER_REPLY_GRACE_SECONDS`;
- `SET_OWNER_REPLY_GRACE_ENABLED`;
- `APPROVE_RESPONSE_REVIEW`;
- `REJECT_RESPONSE_REVIEW`.

`OwnerControlSignal` carries typed parameters and authority evidence. Authenticated owner identity is established independently from message text. This boundary should remain unchanged by semantic interpretation.

Primary implementation locations:

- `attention_router/adapters/wwebjs_owner_control.py`
- `attention_router/application/owner_control.py`
- `attention_router/application/owner_operational_control.py`
- `attention_router/application/owner_automation_control.py`
- `attention_router/application/owner_response_review_control.py`

### 4.2 Agent structured output

`AndyAgentOutput` already separates response text, intent, objective, confidence, missing information, requested actions, requested capabilities, conversation state, and safety flags.

Primary locations:

- `attention_router/application/agents/output.py`
- `attention_router/application/agents/context.py`
- `attention_router/application/agents/instructions.md`

### 4.3 Recent conversational context

Recent bidirectional history is already bounded and remains distinct from persistent memory. It is appropriate for references such as `mais dez`, `na verdade deixa 20`, or linguistic continuity, but it must not replace durable facts.

Primary locations:

- `attention_router/application/decision_pipeline.py`
- `attention_router/application/conversation_language.py`
- ADR `docs/adr/0014-recent-history-vs-persistent-memory.md`

### 4.4 Persistent memory and provenance

The existing archive contains conversation threads, participants, messages, ingestion jobs, candidates, claims, and evidence. Durable claims preserve confidence, source quality, sensitivity, staleness, validity, and provenance.

Primary locations:

- `attention_router/application/memory.py`
- `docs/memory/architecture.md`
- `docs/memory/schema.md`
- `docs/memory/incremental-memory.md`
- `docs/memory/provenance.md`

No parallel memory database should be introduced for this design.

### 4.5 Personal Context and Context Retrieval

`Personal Context V0` is the owner-scoped read model over canonical memory and facts. `Context Retrieval V0` retrieves bounded relevant knowledge deterministically and explicitly leaves room for embeddings or semantic reranking behind the same boundary.

Primary locations:

- `attention_router/application/personal_context.py`
- `attention_router/application/context_retrieval.py`
- `docs/personal-context-v0.md`
- `docs/context-retrieval-v0.md`

### 4.6 Canonical platform context

The platform already models and retrieves:

- timeline/history;
- memory;
- entity state;
- facts;
- capabilities;
- prior decisions;
- effective authority;
- standing directives.

Primary locations:

- `attention_router/core/context.py`
- `attention_router/application/platform/context.py`
- `attention_router/application/platform/entities.py`
- `attention_router/core/entities.py`
- `attention_router/core/events.py`

### 4.7 Relationship and audience

Relationships and audiences are already first-class concepts. They should be extended rather than replaced.

Primary locations:

- `RelationshipRow` and `ActorBindingRow` in `attention_router/infrastructure/models.py`
- `resolve_effective_relationship()` and `resolve_effective_audience()` in `attention_router/application/platform/entities.py`

### 4.8 Preferred names already exist

Persistent memory already recognizes self-declared names and preferred forms of address, including `identity.self_reported_name` and `identity.preferred_name`. Behavior rendering can already use `preferred_name`.

This should become the highest-authority human naming signal when the person explicitly states how they prefer to be addressed. Contact-directory names and provider display names remain lower-authority observations rather than overwriting self-declared preference.

### 4.9 Integration Contract

Integration Contract V0 is already provider-neutral and includes inbound events, artifact receipts, channel delivery, capability invocation, and standardized integration results. WhatsApp and email have already been proven against the same inbound contract without provider-specific core branching.

Primary locations:

- `attention_router/contracts/integration.py`
- `attention_router/application/platform/integrations.py`
- `contracts/integration/v1/`
- `docs/integration-contract-v0.md`
- `docs/integration-adapter-proof-v0.md`

Telegram, SMS, live email providers, Google Calendar, Google Contacts, Home Assistant, and other future providers should be adapters to this architecture, not new product cores.

### 4.10 Android boundary

The Android architecture defines:

`andy-android -> Kotlin SDK -> Client API -> attention-router`

and the authority chain:

`Human Identity -> Tenant Membership -> Active Tenant -> Device -> Session -> SDK/API -> Capabilities / Channels / Integrations`

Device-native capabilities such as location, notifications, camera/QR, microphone, sensors, and future contact access remain typed capabilities. Android permission is not server/tenant authority.

The Android repository already contains the device-identity foundation. Mobile observations therefore have a path to become attributable device evidence rather than anonymous input.

## 5. Proposed layer 1 — Semantic Owner Command V1

### Goal

Allow the authenticated owner to express operational controls naturally without requiring exact phrases.

### Flow

`authenticated owner text`
→ deterministic parser fast path
→ semantic interpreter only if unresolved
→ typed `OwnerControlAction + parameters`
→ existing validation
→ existing authority evidence
→ existing dispatch
→ existing execution/control mutation.

### Example

All of the following may resolve to the same typed action:

- `espera 10`
- `aguarde 10`
- `dá dez segundos antes de responder`
- `coloca a espera em 10 segundos`

Canonical result:

`SET_OWNER_REPLY_GRACE_SECONDS(seconds=10)`

### Constraints

- The semantic interpreter may select only registered owner-control actions.
- It cannot invent a new command type.
- It cannot create authority evidence.
- It cannot bypass parameter bounds.
- Ambiguous unit, target, or effect must produce clarification or rejection.
- The deterministic parser remains as a low-latency explainable fast path.

### Suggested implementation boundary

Create a provider-neutral semantic interpretation service under `attention_router/application/`, not inside `wwebjs_owner_control.py`.

The WWEBJS adapter should remain responsible for transport-specific normalization and authenticated self-chat classification. Semantic interpretation should consume authenticated owner-control input independently of WhatsApp so the same intent contract can later be reused by the Android app, voice, Telegram, or another authenticated owner channel.

## 6. Proposed layer 2 — Relationship Context and Cross-Channel Identity

### Goal

Represent a person independently from the channels through which that person communicates.

### Model

A canonical actor may have multiple external identities:

- WhatsApp account/JID;
- Telegram account;
- SMS/phone identity;
- email address;
- Google/Android contact reference;
- other provider identities.

Threads remain channel-specific for provenance, ordering, retention, and replay semantics. Retrieval and reasoning may project a unified actor-level story when identity resolution is sufficiently confident.

### Identity evidence hierarchy

Identity resolution should distinguish evidence quality. Example ordering:

1. explicit verified/self-declared preference;
2. authenticated account metadata;
3. owner-maintained contact directory;
4. provider display name;
5. contextual inference.

Lower-quality observations must not silently overwrite stronger identity evidence.

### Preferred form of address

If a directory says `Eduardo Albuquerque` but the person explicitly says `pode me chamar de Dudu`, the durable preferred form becomes `Dudu` while preserving the original directory identity as provenance.

### Suggested implementation locations

Extend existing actor binding, memory, relationship, fact, and retrieval projections rather than adding another identity database:

- `ActorBindingRow` / `MemoryActorRow`;
- `RelationshipRow`;
- `MemoryClaimRow`;
- `FactRow`;
- `attention_router/application/platform/entities.py`;
- future actor/relationship context retriever under `attention_router/application/`.

## 7. Proposed layer 3 — Assistant Identity Continuity

### Goal

Andy must not impersonate the represented owner.

The current system already contains assistant identity statements, `FIRST_CONTACT` introduction policy, and recent introduction tracking. This should become a stronger cross-channel identity policy.

### Required behavior

- On the first Andy intervention in an unresolved conversation identity window, clearly identify as Andy/assistant.
- Repeated identification on every turn is unnecessary once identity is established.
- Re-identify after a sufficiently long gap, channel change, identity ambiguity, or other configured boundary.
- Never emit text that implies Andy is the represented owner.
- Preferred name may be used only after actor identity is sufficiently resolved.

### Suggested implementation locations

- `config/andy_behavior_profile.json`;
- `attention_router/domain/behavior.py`;
- `attention_router/application/decision_pipeline.py`;
- future cross-channel conversation identity state if/when separately designed.

## 8. Proposed layer 4 — Initiative Gate

### Goal

Separate internal intelligence from externally visible initiative.

Andy may notice something valuable without immediately speaking about it. Every unsolicited proposal should pass an explicit initiative decision boundary.

### Conceptual inputs

- was Andy directly addressed?;
- was help requested?;
- is the observation about the owner or a third party?;
- is the current message automatic/system-generated?;
- relationship and audience;
- sensitivity;
- urgency;
- confidence;
- interruption cost;
- recent similar proposals or explicit rejection;
- whether the proposed statement creates a commitment or social consequence;
- current owner directives and authority.

### Conceptual outcomes

- `LEARN_SILENTLY`
- `SUGGEST_TO_OWNER_LATER`
- `ASK_OWNER_NOW`
- `RESPOND_TO_CONTACT`

These names are design vocabulary only; implementation names should be fixed during planning.

### Example: third-party automatic message

An automatic message from a clinic may teach Andy the clinic's hours or communication pattern. A linguistic-quality observation about that message should normally remain internal unless the owner directly asks Andy to rewrite or evaluate it.

The architecture should favor learning silently over unsolicited intervention.

## 9. Proposed layer 5 — Consequence Classification

### Goal

Prevent linguistically plausible text from becoming an unauthorized social or operational decision.

A generated response should be classified by consequence before autonomy is granted.

Conceptual classes may include:

- informational acknowledgement;
- clarification request;
- non-committal assistance;
- disclosure of owner state;
- scheduling proposal;
- commitment creation;
- commitment cancellation;
- negotiation/acceptance;
- financial instruction;
- external side effect request.

The exact taxonomy is future implementation work.

### Integration with existing autonomy

Do not create a second approval engine. Consequence classification should feed the existing autonomy/review path:

- `OBSERVE`;
- `REQUIRES_APPROVAL`;
- `AUTO_ALLOWED`.

A response equivalent to `Tudo bem, marcamos outro dia` can alter a social commitment and should not be treated like a harmless acknowledgement unless explicit owner intent or policy authorizes that effect.

Primary existing locations to reuse:

- `attention_router/application/autonomy.py`;
- `attention_router/application/response_review.py`;
- `attention_router/application/owner_response_review_control.py`;
- `attention_router/application/execution.py`;
- existing human approval and execution gates.

## 10. Proposed layer 6 — Pattern Hypothesis Engine

### Goal

Discover useful behavioral patterns from accumulated evidence without treating statistical patterns as facts.

### Operating model

The engine should run as a non-blocking side channel over canonical events, timeline, facts, memory, and entity state.

A hypothesis contains at minimum:

- semantic predicate/type;
- tenant and represented actor;
- evidence references;
- confidence;
- first/last observation;
- recurrence or support count where applicable;
- validity/staleness;
- correlation method/version;
- status such as candidate/accepted/superseded/rejected if a new lifecycle is required.

A first implementation should prefer existing `FactRow`/provenance primitives where they fit. A new table should be introduced only if hypothesis lifecycle semantics cannot be expressed safely through existing facts/claims.

### Example: recurring professional routine

Observations:

- recurring presence at location A around 08:00;
- about one hour at that location;
- recurring travel to location B afterwards;
- calendar entries around the same windows;
- messages referring to `aluno`, `aula`, or appointments.

Possible inference:

`routine.recurring_service_visits`, confidence 0.83, backed by multiple evidence sources.

This is not automatically `profession = personal trainer`. Occupation may become a separate, higher-level hypothesis only when evidence supports it.

### Important constraint

Pattern recognition must distinguish:

- directly observed fact;
- imported historical fact;
- self-reported claim;
- model-generated hypothesis;
- user-confirmed fact.

## 11. Proposed layer 7 — Opportunity Engine

### Goal

Convert stable patterns into optional useful workflows without requiring domain-specific product modules for every profession.

The engine should reason over generic primitives such as:

- person;
- place;
- event;
- time window;
- recurrence;
- value/payment;
- commitment;
- relationship;
- capability;
- device;
- preference;
- authorization.

### Example progression

1. Andy detects recurring visits that resemble appointments.
2. Opportunity Engine proposes `organize recurring schedule`.
3. Initiative Gate decides whether and when to surface it.
4. User accepts.
5. Andy may later offer to associate values with appointments.
6. A future payment capability may allow payment-state tracking, still behind authority and consent.

No `personal_trainer.py` domain module is required for the first version. Useful workflows should emerge from generic primitives and registered capabilities.

## 12. Proposed layer 8 — Semantic Context Retrieval V1

### Goal

Improve retrieval from exact lexical matching to semantic relevance while preserving the existing `Context Retrieval` contract and tenant isolation.

### Requirements

- Keep the current deterministic retrieval as baseline and fallback.
- Add embeddings and/or semantic reranking behind the retrieval boundary.
- Never allow cross-tenant vector search.
- Preserve provenance and relevance explanations.
- Bound candidate count and prompt/context size.
- SECRET/inactive/expired data remains excluded before semantic ranking.
- Semantic retrieval does not grant disclosure authority.

This layer should be measured against Context Retrieval V0 rather than replacing it without evaluation.

## 13. Device and mobile contextual evidence

### 13.1 Location

Suggested mobile acquisition boundary:

`andy-android/capabilities/location`
→ Kotlin SDK
→ authenticated Client API / integration boundary
→ canonical `DEVICE_EVENT`
→ timeline/fact/context projection
→ Pattern Hypothesis Engine.

Location permission on Android only means the application is allowed to read location locally. It is not tenant authority or permission for arbitrary disclosure.

### 13.2 Contacts

Two distinct sources should remain distinguishable:

- Android local Contacts Provider: device capability;
- Google People/Contacts API: provider integration.

Either may provide identity evidence, but neither should silently replace canonical identity or a self-declared preferred name.

### 13.3 Device installation and registration

Device installation/registration should become an attributable event. The system may know that a device was enrolled at time T and begin collecting only the observations authorized from that point forward.

Historical reconstruction is allowed only when an authorized provider explicitly supplies past records such as calendar history, message history, or other imported data. Andy must not fabricate pre-installation history.

## 14. Cross-channel contextual history

The same person may interact through multiple channels. The design keeps two simultaneous truths:

1. each source conversation/thread preserves its original channel and provenance;
2. actor-level reasoning may retrieve related evidence across those channels after canonical identity resolution.

Example:

- yesterday an actor discusses a consultation on Telegram;
- today the same canonical actor asks `pode ser às 15?` on WhatsApp.

The system may retrieve the prior consultation context if identity and relevance are sufficiently strong, without pretending the WhatsApp thread itself contained the Telegram message.

## 15. Data and confidence model

Every future contextual inference should preserve at least:

- `tenant_id`;
- subject/canonical actor;
- predicate/type;
- value or opaque value reference;
- source type;
- source/evidence references;
- confidence;
- observation time;
- validity interval where applicable;
- sensitivity;
- extraction/correlation method version;
- whether the value is observed, self-reported, imported, inferred, user-confirmed, or executed.

Existing `FactClass`, memory source quality, evidence tables, and lineage concepts should be reused wherever possible.

## 16. Privacy and safety boundaries

This architecture increases the amount of context Andy can correlate. Therefore capability growth must not weaken the current privacy model.

Required invariants:

- tenant isolation remains mandatory;
- model output never creates authority;
- raw provider payloads remain outside prompt-facing canonical metadata unless explicitly projected and permitted;
- data minimization precedes model context assembly;
- provenance is retained for durable knowledge;
- sensitive/secret handling remains fail-closed;
- location and contacts are not globally disclosed merely because they were collected;
- third-party context must not automatically become owner Personal Context;
- owner context, other-actor context, and relationship context remain distinguishable;
- high-consequence social, financial, external, or privacy-sensitive effects require appropriate policy or approval;
- users should be able to decline recurring initiative classes without Andy repeatedly re-proposing them.

## 17. Architecture map

```text
Channels / Devices / Providers
WhatsApp | Telegram | SMS | Email | Android | Voice | Calendar | Home Assistant | future providers
        |
        v
Provider adapters / typed device capabilities
        |
        v
Integration Contract / Client API authenticated boundary
        |
        v
Canonical identity + EventEnvelope + Artifact Plane
        |
        +-------------------------+
        |                         |
        v                         v
Conversation archive         Canonical timeline/state/facts
Memory claims/evidence       Relationships/capabilities/authority
        |                         |
        +------------+------------+
                     |
                     v
          Context assembly / retrieval
                     |
          +----------+-----------+
          |                      |
          v                      v
Semantic interpretation   Pattern Hypothesis Engine
          |                      |
          |                Opportunity Engine
          |                      |
          +----------+-----------+
                     |
                     v
                Initiative Gate
                     |
                     v
             Agent structured proposal
                     |
                     v
             Consequence classification
                     |
                     v
     Policy / autonomy / human review / authority
                     |
                     v
       Execution intent / outbox / provider adapter
                     |
                     v
               Delivery evidence
```

## 18. Implementation mapping

| Design concern | Reuse / extend | Likely location |
| --- | --- | --- |
| Natural owner commands | typed Owner Control | new `application` semantic interpreter + existing owner control files |
| Recent conversational references | recent bidirectional history | `application/decision_pipeline.py`, conversation/session context |
| Owner long-term context | Personal Context V0 | `application/personal_context.py` |
| Semantic context search | Context Retrieval V0 | `application/context_retrieval.py` implementation behind stable contract |
| Cross-channel actor identity | ActorBinding / MemoryActor | infrastructure models + application identity resolver |
| Relationship story | RelationshipRow / facts / memory | `application/platform/entities.py` + new relationship-context projection |
| Preferred name | memory claims | existing memory extraction/context + identity evidence hierarchy |
| Andy self-identification | behavior profile / introduced state | `domain/behavior.py`, agent instructions, decision pipeline |
| Unsolicited proposals | new boundary | Initiative Gate in application layer |
| Consequential responses | autonomy/review | consequence classifier feeding existing autonomy |
| Location and device observations | DEVICE_EVENT + Android capability | `andy-android/capabilities/` + Client API/Integration boundary |
| Contacts | Android capability or provider adapter | `andy-android/capabilities/` / `integrations/` |
| Pattern discovery | facts/timeline/provenance | new asynchronous Pattern Hypothesis Engine |
| Feature opportunity discovery | capability registry + patterns | new Opportunity Engine |
| Telegram/SMS/email live | Integration Contract | provider-specific adapters only |

## 19. Recommended delivery sequence

This is architectural sequencing, not an implementation plan.

### Frontier 1 — Semantic Owner Command V1

Solve the exact-command problem while preserving the existing owner-control authority chain.

Success example: `aguarde 10` and `espera 10` resolve to the same typed command after authenticated-owner classification.

### Frontier 2 — Assistant Identity + Initiative Guard

Prevent Andy from impersonating the owner or offering unsolicited interventions in unrelated third-party messages.

### Frontier 3 — Relationship Context + Cross-Channel Identity

Resolve people rather than channels, preserve aliases/provenance, and make preferred forms of address durable.

### Frontier 4 — Device Context V1

Admit authenticated Android location/contact/device observations as canonical evidence without granting disclosure or execution authority.

### Frontier 5 — Pattern Hypothesis Engine V0

Detect recurring patterns as explicit hypotheses with provenance and confidence. No autonomous external action.

### Frontier 6 — Opportunity Engine V0

Generate bounded workflow suggestions from confirmed/high-confidence patterns, gated by initiative policy.

### Frontier 7 — Semantic Context Retrieval V1

Add semantic ranking/embeddings behind the existing deterministic retrieval contract and benchmark against V0.

### Frontier 8 — Additional live channels/providers

Implement Telegram, SMS, email, Google Calendar/Contacts, Home Assistant, and other providers as adapters/capability providers over the established contracts.

## 20. Explicit non-goals for this design document

This document does not:

- implement any of the proposed layers;
- modify runtime behavior;
- change database schema;
- activate providers;
- enable new Android permissions;
- create embeddings or vector indexes;
- merge channels into one physical conversation thread;
- grant automatic authority to inferred facts;
- authorize Andy to make social commitments on behalf of the owner;
- turn location collection into unrestricted tracking or disclosure;
- replace existing owner-control, autonomy, review, or execution state machines;
- prescribe a provider/model vendor for semantic interpretation;
- define production retention periods or legal/privacy policy.

Each frontier requires its own reviewed implementation plan and tests before code changes.

## 21. Architectural invariants to freeze

The following principles should survive provider, model, UI, and transport changes:

1. **Observe != infer.**
2. **Infer != disclose.**
3. **Know != authorize.**
4. **Suggest != execute.**
5. **Model output != authority.**
6. **Channel identity != canonical person identity.**
7. **Android permission != tenant/server authority.**
8. **A plausible reply != an authorized social decision.**
9. **Historical evidence retains provenance.**
10. **Ambiguity fails closed for consequential effects.**

A compact operational formulation is:

**Andy may know a great deal internally while remaining deliberately conservative about what she says and what she does externally.**

## 22. Next planning boundary

The first implementation plan should cover only **Semantic Owner Command V1**. It is the smallest high-value frontier, directly fixes the observed `aguarde 10` versus `espera 10` problem, and can be added without changing the existing authority or execution contracts.

Later frontiers should be planned independently so that identity, initiative, device evidence, pattern discovery, and semantic retrieval remain testable and reviewable as separate subsystems.
