# Andy Cognitive Architecture — Product and Technical Vision

Status: architectural vision  
Scope: Andy / Attention Router / Personal Context V2+  
Tracking roadmap: #219  
V2A implementation: #220  
Follow-on slices: #221–#229  
Canonical repository rule: GitHub is the source of truth.

---

## 1. Purpose

This document consolidates the product and technical vision that motivates the next generation of Personal Context.

The central thesis is simple:

**Andy must not behave like a chatbot that occasionally remembers facts. Andy must maintain a persistent, temporal, explainable representation of the user's world and use that representation to compile the right context when the present requires it.**

The intended progression is:

`observe -> understand -> structure context -> recognize patterns -> form expectations -> detect deviations -> reason -> suggest -> obtain authority -> act -> observe the result -> learn`

Automation is therefore a consequence of understanding, not the starting point.

---

## 2. Product thesis: continuity, not chat memory

The unit of continuity is the human identity, not the chat.

A new chat is only a new interaction surface for the same represented person.

The expected product behavior is:

- the user may return after a week, a year or several years;
- Andy should still represent the same people, relationships, resources, commitments, preferences, ongoing matters and historical context;
- changing the LLM provider must not erase the user's world;
- changing the transport, Android version or plugin must not erase the user's world.

A useful architectural test is:

> If replacing the language model causes Andy to forget who the user is, we built a chatbot.  
> If replacing the language model leaves the user's world intact, we are building a persistent personal intelligence.

The subscription therefore funds more than inference tokens.

It funds **cognitive continuity**:

- storage;
- indexing;
- semantic extraction;
- provenance;
- temporal reasoning;
- context compilation;
- background processing;
- evidence reconciliation;
- graph traversal;
- correction history;
- and later learned graph intelligence.

The product should become more valuable as the user stays with it longer, not because it has more raw messages, but because its representation of the user's world becomes more complete and better corrected.

---

## 3. Andy begins by understanding

A core rule:

**Andy does not begin by trying to automate the user's life. Andy begins by trying to understand it.**

The cognitive order is:

1. observe;
2. identify;
3. relate;
4. place in time;
5. interpret;
6. preserve evidence;
7. consolidate context;
8. recognize recurrence;
9. form expectations;
10. detect anomalies;
11. decide whether the situation deserves attention;
12. compile relevant context;
13. reason;
14. suggest;
15. request authority when needed;
16. act only when authorized;
17. observe the result;
18. update the model.

This order is deliberately conservative.

Knowledge does not imply authority.

Pattern does not imply authority.

Prediction does not imply authority.

Capability does not imply authority.

---

## 4. Raw conversation is not the world model

A message is evidence, not the final semantic memory.

Example raw message:

> "Me manda o boleto desse mês."

The durable semantic interpretation may eventually become:

> this person occupies a user-owned property and periodically requests payment information associated with a recurring rent obligation.

The raw message remains preserved as evidence.

But Andy should not need to reread the same message every time.

The intended pipeline is:

`source -> raw evidence -> normalized event -> episode -> entity/relation/state -> contextual claim -> pattern -> expectation -> current context`

A key statement:

**The conversation is not the context. The conversation updates the context.**

---

## 5. Personal Context Bootstrap

A new user should not have to spend months teaching Andy everything from scratch.

Personal Context V2 introduces the concept of a user-initiated **Personal Context Bootstrap**.

After authentication and explicit authorization, the user may select historical sources that can help build an initial representation of their world.

Possible sources include:

- WhatsApp text;
- Gmail;
- calendar;
- documents;
- contacts;
- photos;
- financial data;
- location history;
- future authorized providers.

The initial Android product may expose an entry point similar to:

> Construir meu contexto inicial  
> Importar conversas do WhatsApp

The backend must remain provider-neutral.

The button does not mean unrestricted access.

It means the user explicitly selected a source and authorized that source for a bounded bootstrap run.

Bootstrap must be:

- asynchronous;
- bounded;
- resumable;
- pausable;
- cancellable;
- idempotent;
- observable;
- tenant scoped;
- non-blocking.

The user may close the application and the backend may continue bounded processing.

---

## 6. Historical bootstrap is not blind brute force

Historical bootstrap should not simply consume all available history without prioritization.

Long-term design may include an information-gain scheduler.

The scheduler asks:

> Is processing more of this source likely to materially improve the current personal context model?

High-value historical regions may include:

- recent active relationships;
- unresolved matters;
- recurring interactions;
- explicit commitments;
- repeated financial or operational patterns;
- frequently referenced people or resources.

Low-yield regions may lose priority.

The objective is not to memorize every character.

The objective is to build a useful and explainable world model.

---

## 7. Example: a portfolio of twelve rental properties

A canonical scenario is a user who manages twelve rental properties.

Andy may eventually understand:

- there are twelve distinct properties;
- each property has a distinct occupant or payer;
- each rent has a different expected amount;
- due dates may differ;
- some obligations are documented;
- others are initially inferred from recurring evidence;
- payments have historical patterns;
- exceptions and extensions occur.

The semantic model must distinguish:

**expected obligation** from **observed payment**.

Example:

`Angelo -> occupies -> Property 07`

`Property 07 -> generates -> Monthly Rent Obligation`

`October Rent -> expected amount -> BRL 1350`

`October Rent -> expected due date -> day 10`

If the due date passes and no authoritative payment evidence exists, the correct state is not necessarily:

`NOT_PAID`

A safer state is:

`UNCONFIRMED_AFTER_DUE`

The wording follows evidence quality.

With only conversational evidence:

> Não encontrei confirmação desse pagamento ainda. Você sabe se ele já pagou?

With a complete, authorized financial source that is known to cover the destination account, the system may support stronger wording.

A permanent rule:

**absence of evidence is not automatically evidence of absence.**

---

## 8. EXPECTED versus OBSERVED

A major cognitive capability is the representation of what is expected to happen.

Traditional systems mostly store what happened.

Andy also needs bounded representations of what **should** happen.

Example:

- expected event: rent payment;
- expected actor: Angelo;
- expected window: around day 10;
- related resource: Property 07.

When the expected window closes:

`EXPECTED != OBSERVED`

This may produce a contextual anomaly.

It does not by itself produce a factual accusation.

It may produce attention.

Example future behavior:

> Dos 12 aluguéis esperados neste mês, encontrei confirmação de 11. O do Ângelo continua sem confirmação. Você tem ciência disso?

No manual reminder had to be programmed.

The observation emerges from:

- relationship context;
- obligation;
- time;
- expected event;
- observed evidence;
- anomaly detection.

---

## 9. Implicit commitments

The same model applies to commitments that were never entered into a calendar.

Example conversation:

> "Sexta-feira eu entrego o documento."

No calendar event exists.

But a semantic commitment may exist:

- actor;
- expected action;
- expected date;
- source evidence;
- confidence;
- related matter.

If Friday passes without evidence of completion, Andy may detect an unresolved expectation.

This is not a traditional reminder.

It is context derived from understanding.

---

## 10. Repeated behavior and suggestions

Another canonical pattern:

A person periodically requests a Pix key and the owner repeatedly sends the same key in the same context.

The progression should be:

- observe repeated requests;
- preserve evidence;
- identify the recurring relationship/context;
- learn a bounded pattern;
- suggest when the same situation appears again.

Example:

> Essa pessoa costuma receber esta chave Pix nesse contexto. Quer que eu envie novamente?

Still no automatic execution.

Execution requires explicit authority.

Future standing directives may authorize narrowly bounded behavior, but that is a later layer and remains separate from inference.

---

## 11. Cognitive Graph

The user's original "neural map" intuition maps well to an explicit **Cognitive Graph**.

The graph represents a living network of:

- people;
- organizations;
- resources;
- properties;
- vehicles;
- accounts;
- documents;
- places;
- relationships;
- events;
- episodes;
- claims;
- facts;
- states;
- commitments;
- obligations;
- expectations;
- patterns.

Example:

`Nilvanda -> MOTHER_OF -> Leonardo`

`Nilvanda -> MOTHER_OF -> Livia`

`Nilvanda -> OWNS -> Property 07`

`Angelo -> OCCUPIES -> Property 07`

`Property 07 -> GENERATES -> Rent Obligation`

This is not initially a neural network.

It is an explicit, auditable graph of the user's world.

---

## 12. PostgreSQL remains valuable

The Cognitive Graph does not imply replacing PostgreSQL.

PostgreSQL remains appropriate for:

- transactional integrity;
- durable state;
- evidence;
- events;
- temporal validity;
- idempotency;
- lifecycle state;
- exact queries;
- authorization data.

The Cognitive Graph is initially a semantic projection over source-of-truth rows.

A useful distinction:

PostgreSQL answers:

> What exact payment record exists for this obligation?

The Cognitive Graph helps answer:

> Who is Angelo, what connects him to this property, and why is this message about money relevant now?

A dedicated graph database should only be introduced later if measured traversal or scale requirements justify it.

Graph semantics come before graph-database migration.

---

## 13. Symbolic first, neuro-symbolic later

The initial architecture is deliberately symbolic and explicit.

Symbolic means important knowledge has explicit representation:

- person;
- relationship;
- resource;
- event;
- temporal validity;
- provenance;
- confidence;
- state;
- claim.

It does not mean a pile of hardcoded business-condition branches.

The intended evolution is:

1. explicit symbolic graph;
2. deterministic graph algorithms;
3. graph-aware context compilation;
4. embeddings and semantic similarity;
5. specialized learned models;
6. Graph Neural Networks and temporal graph models when justified.

The future architecture is therefore **neuro-symbolic**.

The neural layer augments the explicit model.

It never becomes the sole owner of truth.

---

## 14. GNN: what it is and what it is not

GNN means **Graph Neural Network**.

A GNN is:

- not a protocol;
- not a database;
- not the canonical organization standard for all data;
- not a complete theory of cognition.

It is a family of machine-learning architectures designed to learn from graph-structured data.

The conceptual stack is:

`graph theory -> graph representation -> knowledge graph -> graph machine learning -> GNN -> specific architectures`

Examples of specific graph architectures include:

- GCN;
- GraphSAGE;
- GAT;
- R-GCN.

For Andy, relational or heterogeneous graph models are especially interesting because relations have different meanings:

- MOTHER_OF;
- OWNS;
- OCCUPIES;
- PAYS;
- WORKS_AT;
- PARTICIPATES_IN.

---

## 15. Message passing and graph embeddings

Many GNN architectures use message passing.

A node receives information from neighboring nodes, aggregates it, and updates its representation.

Multiple layers allow information to propagate further through the graph.

A learned node representation may become an embedding.

Structurally similar people or situations may become mathematically similar even if their raw text differs.

Possible future uses include:

- link prediction;
- role classification;
- anomaly scoring;
- graph relevance ranking;
- episode association;
- structural similarity;
- temporal next-event prediction.

---

## 16. Why GNN is not V2A

Andy should start with a graph immediately.

Andy should not make a GNN the mandatory foundation immediately.

A GNN requires:

- stable graph semantics;
- meaningful node and edge types;
- corrected identities;
- real relationships;
- labels;
- examples;
- accepted and rejected hypotheses;
- temporal evidence;
- evaluation datasets;
- clear learning tasks.

Without those, a sophisticated learned model only learns noisy structure more efficiently.

A core strategy is:

**the first Andy produces the dataset that can train later Andy models.**

Every owner correction helps.

Every confirmed relation helps.

Every rejected merge helps.

Every accepted/rejected hypothesis helps.

Every useful/irrelevant suggestion helps.

---

## 17. Learned inference is never canonical truth by itself

A permanent safety rule:

`learned output -> Candidate Insight`

Never:

`learned output -> canonical truth -> execution`

Example future model output:

> 0.91 probability that these two source identities refer to the same person.

That becomes a candidate.

It may become:

- confirmed;
- rejected;
- ambiguous;
- superseded.

The model does not silently merge identities.

Likewise, a GNN prediction does not create execution authority.

---

## 18. Known GNN limitations

GNNs do not solve every graph reasoning problem.

Relevant limitations include:

**Over-smoothing**

After too much message passing, node representations may become too similar.

**Over-squashing**

Large amounts of distant information may be compressed into small representations, making long-range dependencies hard to preserve.

These issues matter because personal context may span years and many relational hops.

Therefore graph neural models are one future engine, not the cognitive architecture itself.

---

## 19. Spreading activation

A useful non-neural graph technique for Andy is **spreading activation**.

Example stimulus:

> "Ângelo ainda não mandou nada?"

Seed concepts:

- Angelo;
- payment.

Activation propagates through nearby graph edges.

High-relevance neighborhood may include:

- Property 07;
- rent obligation;
- current period;
- Nilvanda;
- recent payment history.

Distant information receives little or no activation.

This supports bounded context retrieval without loading the user's entire history.

---

## 20. Context Compiler

The Context Compiler is a central future component.

Its task is not to remember everything.

Its task is to compile the smallest useful context for the current decision.

Example request:

> "Ângelo ainda não mandou nada?"

A compiled packet may include:

- Angelo identity;
- relationship;
- related property;
- current obligation;
- amount;
- due date;
- recent fulfillment history;
- current anomaly state;
- relevant owner preferences;
- current authority boundary.

This may fit in hundreds or a few thousand tokens.

But it may be compiled from years of history.

The large memory stays outside the active model context.

---

## 21. Multi-source sensing

The architecture should not treat external channels as isolated tool calls.

A better model is:

**many sensors observing one world.**

Examples:

- WhatsApp observes conversations and relationships;
- Gmail observes contracts, notifications and commitments;
- Calendar observes future intent;
- GPS observes movement;
- Financial sources observe settlement;
- Documents observe formal facts;
- Photos observe places and objects;
- Contacts observe part of the social graph;
- Mobile device state observes local operational context.

This relates to **Data Fusion / Information Fusion**.

Each source has a partial view.

The cognitive system reconciles those partial views.

---

## 22. Emergent knowledge

One of Andy's strongest future behaviors may be producing useful contextual knowledge that no source stated explicitly.

Example:

WhatsApp indicates recurring rent conversation.

Financial source shows no matching settlement.

Calendar/time indicates the due window passed.

The graph knows Angelo occupies the property.

History knows the expected pattern.

No source contains the sentence:

> "The Angelo rent is currently anomalous."

The knowledge emerges from correlation.

This is powerful but dangerous if poorly governed.

Therefore every conclusion needs:

- provenance;
- source quality;
- temporal validity;
- confidence;
- contradiction awareness;
- evidence independence awareness.

---

## 23. Evidence fusion

Multiple sources do not automatically mean multiple independent confirmations.

Example:

A WhatsApp message says:

> "Recebi o boleto por email."

The email contains the boleto.

Those may be two observations of the same underlying event, not two independent proofs.

Future Evidence Fusion must track:

- source;
- upstream origin;
- dependency;
- duplication;
- contradiction;
- confidence;
- completeness;
- coverage window.

The objective is to avoid false confidence caused by duplicated evidence.

---

## 24. Cognitive Loop

The long-term Cognitive Loop is:

`PERCEIVE`

`IDENTIFY`

`RELATE`

`TEMPORALIZE`

`INTERPRET`

`STORE EVIDENCE`

`CONSOLIDATE`

`UPDATE WORLD MODEL`

`DETECT PATTERN`

`FORM EXPECTATION`

`COMPARE EXPECTED vs OBSERVED`

`RAISE ATTENTION`

`COMPILE CONTEXT`

`REASON`

`SUGGEST`

`REQUEST AUTHORITY`

`ACT`

`OBSERVE RESULT`

`LEARN`

Then the cycle repeats.

This loop is broader than any individual LLM call.

---

## 25. Opinion as contextual evaluation

A useful Andy "opinion" should not be a generic LLM opinion.

Example:

> "Ângelo pediu para pagar só no dia 20. O que você acha?"

A contextual evaluation may consider:

- relationship duration;
- payment regularity;
- previous delays;
- unresolved debt;
- usual owner behavior;
- historical exception handling;
- current obligations;
- uncertainty.

The result is better described as:

**explainable contextual evaluation**

rather than generic opinion generation.

---

## 26. Tools are capabilities, not cognition

PDF, Excel, DOCX, MP3 conversion, calendar updates and external actions are capabilities.

They are important but are not the cognitive heart.

The intelligence appears when Andy understands when a capability would be useful.

Example:

Andy notices multiple property-sale proposals spread across conversations and documents.

It may suggest:

> Quer que eu monte uma planilha comparando essas propostas?

Only after confirmation should the Excel capability execute.

The tool is an arm.

The Cognitive Graph and Cognitive Loop provide the context that makes the arm useful.

---

## 27. Existing repository foundation before V2

The repository already contained strong foundations before Personal Context V2.

Certified primitives include:

### Identity and conversation

- `ActorBindingRow`;
- `MemoryActorRow`;
- `ConversationThreadRow`;
- `ConversationParticipantRow`;
- `ConversationMessageRow`.

### Historical ingestion

- `HistoryAdapter`;
- `HistoryBackfillService`;
- resumable cursors;
- dry-run;
- archive deduplication;
- secret redaction;
- memory candidate/promotion metrics.

### Memory/evidence

- `MemoryExtractionRunRow`;
- `MemoryCandidateRow`;
- `MemoryClaimRow`;
- `MemoryEvidenceRow`;
- `MemoryIngestionJobRow`.

### World and temporal primitives

- `ResourceRow`;
- `RelationshipRow`;
- `TimelineEventRow`;
- `FactRow`;
- `EntityStateRow`.

### Authority

- capability registry/provider boundary;
- `ExecutionIntentRow`;
- `HumanExecutionAuthorizationRow`;
- client approval;
- policy/provider revalidation;
- controlled execution/materialization.

This means V2 does not begin from an empty repository.

---

## 28. Personal Context V1A–V1R

The existing Personal Context lineage already implements:

- V1A: temporal recurrence;
- V1B: governed hypothesis persistence;
- V1C: bounded recommendation;
- V1D: recommendation lifecycle;
- V1E: authority revalidation;
- V1F: governed materialization;
- V1G: runtime orchestration;
- V1H: owner correction;
- V1I: explicit recommendation reply;
- V1J: authority runtime;
- V1K: materialization runtime;
- V1L: relationship-scoped recurrence;
- V1M: event-sequence hypotheses;
- V1N: missing expected step anomaly;
- V1O: anomaly suggestion;
- V1P: suggestion delivery/lifecycle;
- V1Q: bounded structural review;
- V1R: owner controls.

V1N is particularly important because it already codifies the invariant:

**absence is inference, never fact.**

V2 extends this philosophy.

---

## 29. Personal Context V2 roadmap

Parent roadmap:

- #219 — Personal Context V2 — Cognitive Graph and Semantic Bootstrap

Implementation slices:

- #220 — V2A Cognitive Graph Foundation
- #221 — V2B Semantic Bootstrap Run Contract
- #222 — V2C Cross-source Entity Resolution V0
- #223 — V2D Semantic Episode Builder V0
- #224 — V2E Candidate Insight and Semantic Consolidation
- #225 — V2F Graph-aware Context Compiler V1
- #226 — V2G Obligation and Expectation Model V0
- #227 — V2H Cognitive Attention and Salience Engine V0
- #228 — V2I Graph Intelligence V0
- #229 — V2J Learned Graph Intelligence Readiness

---

## 30. V2A — Cognitive Graph Foundation

V2A is already implemented and merged.

It defines:

- `CognitiveNode`;
- `CognitiveEdge`;
- `CognitiveGraphSlice`;
- node kinds;
- relation kinds;
- inference classes.

Initial node kinds include:

- PERSON;
- ENTITY;
- RESOURCE;
- RELATIONSHIP;
- EVENT;
- CLAIM;
- FACT;
- STATE.

V2A projects existing source-of-truth rows into a read-only graph.

It does not:

- invent relationships;
- merge identities;
- create facts;
- create claims;
- create execution intents;
- enqueue outbound work.

It is symbolic, tenant-scoped, temporal and provenance-aware.

---

## 31. V2B — Semantic Bootstrap

V2B turns existing history backfill into an explicit product lifecycle.

Core concepts:

### BootstrapRun

One owner-scoped historical bootstrap lifecycle.

It contains:

- tenant;
- owner identity;
- represented owner actor;
- source kind;
- source account;
- source revision;
- source selection;
- consent reference;
- idempotency key;
- processing budget;
- progress;
- lifecycle state;
- resume cursor;
- timestamps.

States:

- CREATED;
- QUEUED;
- RUNNING;
- PAUSED;
- COMPLETED;
- CANCELLED;
- FAILED.

### BootstrapBatch

A bounded unit of processing with:

- ordinal;
- cursor before/after;
- metrics;
- idempotency key;
- lifecycle state;
- timestamps.

Pause/cancel stop future batches but do not erase already-ingested evidence.

The initial source kind is `WHATSAPP_TEXT`, but the contract remains provider-neutral.

---

## 32. V2C — Cross-source Entity Resolution

Entity resolution must answer questions such as:

> Does "Ângelo" in WhatsApp refer to the same real person as "Angelo Silva" in a contract and "ANGELO SILVA" in a bank descriptor?

The answer may be uncertain.

Required states include:

- PROPOSED;
- CONFIRMED;
- REJECTED;
- AMBIGUOUS;
- SUPERSEDED.

Signals may include:

- explicit provider identities;
- normalized phone/email when permitted;
- owner-confirmed aliases;
- stable source identifiers;
- corroborating relationship evidence.

One similar name is not sufficient for silent merge.

A rejected merge must remain durable evidence against immediate repeated proposals from the same evidence.

---

## 33. V2D — Semantic Episode Builder

Low-level messages and events should be grouped into durable situations.

Examples:

- sale of one property;
- recurring rental matter;
- vehicle purchase negotiation;
- job interview process;
- administrative case.

An Episode preserves:

- participants;
- resources;
- start/end/last activity;
- lifecycle;
- evidence membership;
- provenance;
- confidence;
- temporal state;
- sensitivity.

Raw evidence remains immutable.

Episode grouping is semantic organization, not execution authority.

---

## 34. V2E — Candidate Insight

The boundary between interpretation and canonical context is **Candidate Insight**.

A Candidate Insight contains:

- proposed subject/predicate/object;
- evidence references;
- source episode;
- source engine;
- confidence;
- temporal scope;
- sensitivity;
- contradiction refs;
- lifecycle.

Possible states:

- PROPOSED;
- ADMITTED;
- REJECTED;
- NEEDS_REVIEW;
- SUPERSEDED.

An LLM, embedding model or future GNN may produce Candidate Insights.

It may not directly write canonical truth.

---

## 35. V2F — Graph-aware Context Compiler

The existing lexical retrieval remains a useful deterministic baseline.

V2F adds graph-aware retrieval:

- seed entity/concept resolution;
- bounded graph traversal;
- hop/fan-out limits;
- relationship filters;
- recency;
- temporal validity;
- confidence;
- provenance;
- owner privacy controls;
- context budget;
- explanation paths.

Retrieval means only:

**relevant knowledge selected for reasoning.**

It is not disclosure authority and not execution authority.

---

## 36. V2G — Obligation and Expectation Model

V2G formalizes what is expected to occur.

Core concepts include:

- recurring definition;
- expected actor;
- expected event/outcome;
- resource;
- cadence;
- due rule;
- obligation instance;
- partial fulfillment;
- extension;
- waiver;
- reconciliation.

Suggested states:

- EXPECTED;
- SATISFIED;
- PARTIALLY_SATISFIED;
- EXTENDED;
- WAIVED;
- UNCONFIRMED_AFTER_DUE;
- SUPERSEDED.

Again:

**UNCONFIRMED_AFTER_DUE != NOT_PAID**

---

## 37. V2H — Cognitive Attention and Salience

Not every anomaly deserves user attention.

Salience may consider:

- impact;
- urgency;
- novelty;
- confidence;
- temporal proximity;
- relationship relevance;
- owner relevance;
- expectation violation;
- recurrence stability;
- source quality;
- prior acknowledgment;
- suppression state.

Possible outcomes:

- ignore;
- queue for reasoning;
- generate bounded suggestion.

Attention is not execution authority.

---

## 38. V2I — Graph Intelligence

Graph Intelligence is the pluggable layer that operates over the explicit graph.

Initial engines may include:

- bounded traversal;
- spreading activation;
- structural similarity;
- rule-based link candidates;
- anomaly features;
- path ranking.

Stable semantic interfaces should permit later replacement.

Examples:

- EntityResolver;
- RelationCandidateEngine;
- RelevanceEngine;
- AnomalyEngine;
- ContextPathRanker.

Every learned or heuristic output remains explainable and bounded.

---

## 39. V2J — learned graph readiness

V2J exists specifically to prevent premature GNN adoption.

Before introducing a learned graph model we need:

- stable graph semantics;
- organic versus synthetic lineage;
- owner-confirmed relations;
- rejected candidates;
- corrected identities;
- superseded relations;
- temporal holdout data;
- deterministic baselines;
- task-specific evaluation;
- leakage prevention;
- calibration;
- explainability;
- false-positive budgets.

Potential tasks:

- link prediction;
- role classification;
- graph anomaly scoring;
- relevance ranking;
- episode association;
- temporal next-event prediction.

Any learned model should begin in shadow mode.

---

## 40. Core invariants

The following are architecture-level invariants.

**GitHub is the source of truth.**

`GitHub -> PR/CI -> exact SHA -> AGT validation -> optional deploy`

**Knowledge is not authority.**

**Inference is not fact.**

**Absence is not fact.**

**Raw evidence remains traceable.**

**Owner correction outranks older inference.**

**A learned model may propose; it does not silently canonicalize truth.**

**A new chat does not create a new user identity.**

**Context must be compiled, not brute-force loaded.**

**Neural intelligence augments the symbolic graph; it does not erase it.**

**Provider integration is a sensor, not the center of the architecture.**

**Execution remains downstream from authorization.**

---

## 41. Long-term architecture

Conceptually:

`Sources / Sensors`

WhatsApp, Gmail, Calendar, GPS, financial providers, documents, photos, voice,
contacts and future providers.

Then:

`Normalization / Evidence Store`

Then:

`Personal Temporal Cognitive Knowledge Graph`

Then multiple intelligence engines:

`Rules + Graph Algorithms + LLM + Similarity + future Embeddings/GNN`

Then:

`Candidate Insights / Hypotheses`

Then:

`Semantic Consolidation`

Then:

`Patterns / Expectations`

Then:

`Attention / Salience`

Then:

`Context Compiler`

Then:

`Reasoning`

Then:

`Suggestion`

Then:

`Authority / Approval`

Then:

`Action`

The result returns as new evidence.

This creates a continuous cognitive loop.

---

## 42. Product identity

The differentiator is not "Andy has GPT".

The differentiator is:

**Andy maintains a persistent model of this person's world.**

Models can change.

Providers can change.

Phone hardware can change.

Transports can change.

What should persist is the user's unique cognitive context accumulated over years.

That is the product moat.

That is the reason a subscription can justify long-term storage and processing.

That is the reason Andy should feel increasingly useful over time.

---

## 43. Final formulation

The intended Andy can be summarized as follows:

**Andy is a persistent personal cognitive architecture that observes multiple authorized sources, preserves evidence, builds a temporal graph of the user's world, recognizes patterns and expectations, compiles only the relevant context for the current situation, reasons with uncertainty, suggests before acting, and keeps execution authority separate from knowledge.**

And the most important product sentence is:

**Andy does not start by automating the user's life. It starts by understanding it.**
