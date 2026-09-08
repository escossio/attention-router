# Andy Enterprise: future vision

> FUTURE EVOLUTION / NOT IMPLEMENTED. All enterprise modules and workflows below are conceptual proposals, not current product capabilities or commitments. All people, institutions, money amounts and scenarios are fictional examples. No organization or employee is being described.

| Implemented foundations | Future concepts |
| --- | --- |
| Policy/review/execution boundaries, recent conversation context, optional voice, memory controls and audit provenance | Enterprise roles, operational session state, ledgers, reconciliation, versioned goals, performance coaching and management workflows |

This design must not be interpreted as authorization for employee monitoring or automated high-impact decisions. Governance, necessity, human review and minimization are prerequisites.


> Status: living design document. This is not a contract, policy, legal specification, or implementation commitment. It records product and architecture directions to preserve the reasoning and make future iterations easier.

## 1. Vision

Andy Enterprise extends Andy from a personal contextual agent into an enterprise copilot that understands a person's organizational identity, formal role, active operational session, authority, procedures, goals, execution evidence, and performance context.

The core idea is not to replace human interaction. It is to reduce operational friction, standardize routine guidance, preserve provenance, improve decision quality, and surface situations where human judgment is more valuable than procedural lookup.

The model should keep the following concepts separate:

- **Person**: the employee as an individual.
- **Organization**: the company or institution where the employee operates.
- **Formal role**: the employee's official position.
- **Operational session**: the role/function being exercised at a specific moment.
- **Authority**: what the employee is allowed to know, decide, authorize, or execute in that session.
- **Evidence**: what actually happened in systems and approved corporate channels.
- **Policies and procedures**: the rules in force at the time of an action.
- **Goals and incentives**: the performance plan in force at that time.

This separation is the foundation for every capability below.

## 2. Enterprise Context

The enterprise context is a dynamic occupational context layered on top of Andy's persistent relationship with the person.

An employee entering a bank as a teller could receive a context package containing:

- teller procedures;
- operational codes;
- system navigation;
- required documents;
- authorization limits;
- escalation rules;
- exceptions;
- current procedures and revisions;
- the boundaries of what that employee may consult or execute.

If the employee is promoted, the person remains the same, while the organizational role and accessible operational context evolve.

This enables Andy to accompany a professional trajectory without conflating personal identity with organizational authority.

## 3. Operational Copilot

The first Enterprise capability is a role-aware operational copilot.

Examples:

- "Which code do I use for this operation?"
- "What is the procedure for this exception?"
- "Which documents are mandatory?"
- "Do I need a supervisor's authorization above this amount?"

Andy should answer from the procedure version that is actually in force for that employee, role, unit, and context.

When there is no reliable procedure, Andy should say so and escalate instead of fabricating guidance.

## 4. Authority and Disclosure Guard

Enterprise knowledge must not be treated as a flat corpus available to every employee.

Andy should distinguish:

- what the employee is allowed to know;
- what the employee may execute;
- what requires explicit authorization;
- what belongs to another role;
- what must be escalated to a manager, compliance, HR, security, or another authority.

The same knowledge base can therefore produce different answers depending on the requesting employee's authority.

This is a natural extension of Attention Router's policy, authorization, disclosure, and context boundaries.

## 5. Role Scope Guard

A major Enterprise capability is preserving coherence between the formal role and the work actually performed.

The Role Scope Guard should classify activities against a role model such as:

- `IN_SCOPE`
- `TEMPORARY_DELEGATION_ALLOWED`
- `AUTHORIZATION_REQUIRED`
- `ROLE_SCOPE_MISMATCH`
- `UNKNOWN_REQUIRES_REVIEW`

The purpose is preventive governance, not automatic accusation.

Example:

An employee is repeatedly performing tasks normally associated with another role. Andy can detect the pattern and ask whether this is a temporary delegation, whether there is formal authorization, or whether the employee's actual role should be reviewed.

This can protect both employee and company by turning a problem that is normally discovered retrospectively into something that can be clarified while it is happening.

The system should preserve symmetric evidence. It must not be designed only to defend the employer. If an employee repeatedly performs out-of-scope activities, that fact must remain visible as evidence rather than being erased by a procedural acknowledgment.

## 6. Operational Session Context

The formal role and the function exercised at a particular moment must be separate concepts.

Example:

A branch operational manager sees the teller queue exceed a normal threshold and temporarily sits at a teller station to restore service flow.

The employee remains an operational manager, but creates a temporary teller operational session.

A session can contain:

- employee identity;
- formal role;
- role exercised in this session;
- branch/unit;
- terminal/workstation;
- start timestamp;
- end timestamp;
- authority granted for the session;
- reason for temporary role assumption;
- procedure versions in force;
- goal plan version in force;
- operational events executed during the session.

This session becomes a bounded context for audit, assistance, reconciliation, and performance interpretation.

## 7. Operational Ledger

Every relevant event performed in an operational session can be linked to that session and employee.

The Operational Ledger is not merely a log dump. It should preserve structured, interpretable evidence with provenance.

Possible events include:

- cash receipts and disbursements;
- reversals;
- cancellations;
- authorizations;
- product proposals;
- product activations;
- system errors;
- exception paths;
- approvals;
- transactional timestamps;
- relevant channel events.

The goal is to let Andy reason over what happened without losing the distinction between fact, inference, and recommendation.

## 8. Reconciliation Copilot

The Operational Ledger enables a session-scoped forensic/reconciliation copilot.

Example:

At the end of the day an employee discovers a teller difference of R$ 2,342.26 and asks:

"Andy, where is the most likely place an error occurred that caused this difference?"

Andy should not declare guilt or certainty. It should rank plausible causes from the evidence available.

It can search for patterns such as:

- exact amount matches;
- half/double relationships;
- combinations of two or more events;
- sign inversion;
- reversal without expected counter-event;
- duplicate posting;
- cancellation after physical cash movement;
- decimal-position errors;
- digit transposition;
- anomalous operation sequences;
- temporary operator changes;
- exceptional authorization paths.

The output should be something like:

- likely candidate events;
- evidence supporting each candidate;
- relative confidence;
- suggested review order.

This is observability applied to human operational work: person + terminal + procedure + system + physical process.

## 9. Preventive Reconciliation

The same engine can work before the final closing.

During a session, Andy may detect a condition likely to create a later discrepancy and suggest review before the error compounds.

This should be thresholded carefully to avoid interrupting the operator constantly. The design should optimize signal quality, not alert volume.

## 10. Goal Engine

Enterprise sessions can also become the authoritative attribution boundary for products and performance goals.

When a product is effectively completed in a bank system, that event can be linked automatically to:

- employee;
- operational session;
- branch/unit;
- product;
- campaign;
- goal plan version;
- points/weights;
- attribution rules.

This eliminates dependence on manual self-reporting of production.

## 11. Versioned Goal Plans

Goals change over time. Therefore performance rules must be versioned and time-bound.

A product must never simply mean "10 points" globally.

Instead:

- Product X = 10 points under Goal Plan v17 from date A to date B;
- Product X = 15 points under Goal Plan v18 from date C onward;
- campaign multipliers and retroactive adjustments are explicit rule changes.

The event remains immutable. The rule interpretation is versioned.

This provides reproducible answers to questions such as:

- "Why did this event contribute this score?"
- "Which plan was in force?"
- "Was this change retroactive?"

## 12. Performance Ledger

The Performance Ledger is the projection of operational events under goal rules.

It should support multiple simultaneous dimensions:

- individual goal;
- team goal;
- branch goal;
- regional goal;
- campaign goal;
- product-mix goal;
- monthly period;
- quarterly period;
- other time windows.

A single product event can contribute to multiple goal projections without being duplicated as multiple facts.

The event is one. Attribution rules create the projections.

## 13. Performance Coach

Andy can expose performance context directly to the employee.

Examples:

- current points vs expected trajectory;
- forecast at the end of the period;
- categories above or below pace;
- required remaining units;
- recent trend;
- areas where additional training may help.

This can reduce uncertainty and transform performance management from a last-week-of-the-month reaction into continuous guidance.

The employee should see enough evidence to understand the calculation rather than being shown a mysterious score.

## 14. Management Copilot

Managers need a different view of the same evidence.

Examples:

- "How is the branch tracking against this month's goal?"
- "Which categories are below required pace?"
- "Who needs coaching, and in which product?"
- "Who is already above trajectory and does not need additional pressure?"
- "If nothing changes, where will the branch finish at period end?"

The goal is not just dashboards. Andy should interpret the data, explain why a trend matters, and identify where management attention has the highest expected value.

## 15. Opportunity vs Conversion

A useful management distinction is separating poor performance caused by lack of opportunity from poor performance caused by low conversion.

Two employees with the same product count may have very different operational contexts.

Andy could compare:

- eligible customer opportunities;
- product presentations;
- customer acceptance;
- final activation;
- later cancellation/reversal.

This enables better coaching and avoids blaming an employee for a lack of opportunity.

## 16. Career Intelligence

Role Scope, Operational Ledger, and Performance evidence can also support professional development.

If an employee progressively and legitimately performs tasks associated with the next level, Andy can identify competency development.

Examples:

- repeated successful execution of higher-complexity procedures;
- sustained reduction in reconciliation errors;
- increased operational autonomy;
- consistent performance quality;
- successful temporary-role sessions.

This should not automatically decide promotions. It can provide evidence that the employee may be ready for evaluation.

## 17. Contextual Training

Andy can use repeated assistance requests as a signal for targeted training.

Example:

The employee asks about the same reconciliation procedure several times over two weeks.

Andy can offer a compact explanation or refresher specifically on that topic.

This creates contextual learning rather than generic training detached from real work.

When procedures change, Andy can highlight only what changed instead of forcing employees to rediscover differences in a long document.

## 18. Goal Integrity Engine

A major gap in traditional performance systems is that they often measure the counted event but not whether the event represents the objective the metric was designed to create.

The Goal Integrity Engine addresses the difference between:

- **Metric**: the event that produces a point.
- **Objective**: the real business outcome the institution intended.

Example:

A goal may intend to increase legitimate adoption of a product by appropriate customers, while the operational metric only counts activations.

Those are not equivalent.

A metric can be gamed while appearing numerically successful.

## 19. Closed-Loop Goal Attribution

A more complete goal model links:

1. Goal Plan
2. Employee Context
3. Operational Session
4. Customer Interaction
5. Customer Decision
6. Product Event
7. Post-sale Outcome
8. Goal Attribution
9. Integrity Analysis
10. Goal Design Feedback

This closes a loop that is often open between customer interaction and the final KPI.

## 20. Customer Interaction Evidence

Some forms of goal gaming are possible because the institution sees the product activation but has weak visibility into how the customer interaction produced it.

When customer interactions happen through approved corporate channels, the system can preserve evidence such as:

- proposal presented;
- eligible product;
- timestamp;
- channel;
- consent signal;
- acceptance;
- activation;
- reversal/cancellation;
- subsequent product persistence;
- relevant structured interaction events.

The design should prefer structured evidence and minimization over storing unnecessary raw communications.

Semantic analysis of communications should require a stronger governance boundary and should only be used where appropriate, permitted, transparent, and necessary.

## 21. Quality of Production

Two employees can both have 100 points while producing very different business quality.

The system should be able to distinguish, for example:

- sustainable activations;
- early cancellations;
- reversals;
- concentration in a single high-scoring product;
- unusual end-of-period spikes;
- repeated patterns that deserve review.

Possible integrity states include:

- `VALIDATED`
- `PENDING_QUALITY_WINDOW`
- `ANOMALOUS_PATTERN`
- `REVERSAL_ASSOCIATED`
- `ATTRIBUTION_UNCERTAIN`
- `MANUAL_REVIEW_REQUIRED`

Avoid automatic labels such as `FRAUD=YES` based only on statistical signals.

Andy should surface evidence and anomalies for human review.

## 22. Quality-Adjusted Goal View

A future goal system may expose more than raw points.

Examples:

- gross score;
- validated score;
- score under observation;
- persistence rate;
- cancellation rate;
- quality indicators;
- attribution confidence.

Any consequence for compensation, performance evaluation, or employment decisions requires explicit governance, human review, and applicable legal/HR controls.

## 23. Protecting Ethical Employees

A quality/integrity model is not only an anti-abuse mechanism.

It can also correct an unfair incentive where an employee following proper procedures appears equivalent to an employee using shortcuts.

Good operational behavior should become visible in the evidence rather than being treated as an invisible moral cost.

## 24. Goal Design Feedback

The organization should be able to ask whether the goal itself is producing the desired behavior.

Examples:

- a branch greatly exceeds a target but has unusually high early cancellations;
- adoption spikes at period close and collapses immediately afterward;
- a scoring structure causes excessive concentration in one product;
- units with similar customer profiles show extreme differences in acceptance;
- the metric increases while the business objective does not.

This enables Andy to say, in effect:

**The metric improved, but the objective did not.**

That is not employee-level optimization. It is organizational learning.

## 25. Organizational Feedback Loop

The highest-level Enterprise capability is a loop in which the institution learns from the gap between formal design and operational reality.

Questions include:

- Are employees regularly performing tasks outside their formal roles?
- Are temporary delegations becoming permanent in practice?
- Are job descriptions outdated?
- Are procedures causing repeated operational errors?
- Are goals producing unintended incentives?
- Are training programs aligned with the difficulties employees actually face?
- Are performance rules rewarding quality or only countable activity?

The system should help the organization revise roles, procedures, training, and goals from evidence instead of anecdote alone.

## 26. Governance Principles

Andy Enterprise must not become a generalized employee-surveillance system.

Core principles:

### 26.1 Purpose limitation
Data collected for operational assistance should not automatically become evidence for unrelated purposes.

### 26.2 Transparency
Employees should understand which enterprise contexts, events, and channels are being used.

### 26.3 Data minimization
Prefer structured events over unnecessary raw content.

### 26.4 Provenance
Every significant claim should retain evidence showing where it came from.

### 26.5 Temporal correctness
Procedures, roles, authority, and goal plans must be interpreted according to the version in force at the relevant time.

### 26.6 Human review
Anomalies are not verdicts. High-impact employment, compliance, disciplinary, compensation, or customer decisions require appropriate human authority.

### 26.7 Symmetric evidence
The evidence model should not selectively preserve only facts favorable to the institution or only facts favorable to the employee.

### 26.8 Separation of personal and organizational context
The person's persistent Andy identity and the organization's enterprise context must remain separate authorization domains.

### 26.9 Revocation
When a person leaves an organization or loses a role, enterprise access must be revocable without destroying unrelated personal context.

### 26.10 No dark-pattern goal optimization
Andy must not recommend unsuitable products simply because they produce more points. Customer eligibility, suitability, consent, and policy constraints come before goal optimization.

## 27. Conceptual Modules

The current conceptual map is:

1. **Enterprise Context** - organizational identity and role context.
2. **Operational Copilot** - procedural assistance.
3. **Authority & Disclosure Guard** - knowledge/action access control.
4. **Role Scope Guard** - role-vs-work coherence.
5. **Session Context** - temporary operational role and bounded session state.
6. **Operational Ledger** - structured evidence of what happened.
7. **Reconciliation Copilot** - discrepancy analysis and ranked hypotheses.
8. **Preventive Reconciliation** - early detection of likely operational divergence.
9. **Goal Engine** - versioned performance rules.
10. **Performance Ledger** - event attribution to goals.
11. **Performance Coach** - employee-facing progress guidance.
12. **Management Copilot** - manager-facing interpretation and projection.
13. **Career Intelligence** - evidence of competency evolution.
14. **Contextual Training** - targeted learning from operational need.
15. **Goal Integrity Engine** - metric-vs-objective integrity.
16. **Closed-Loop Goal Attribution** - customer interaction through post-sale outcome.
17. **Organizational Feedback Loop** - learning about roles, procedures, incentives, and goals.

## 28. Example End-to-End Scenario

A branch operational manager temporarily assumes a teller station because queue pressure exceeds the normal threshold.

1. A new operational session is opened.
2. The employee remains formally an operational manager.
3. The session role is teller.
4. Teller-specific procedures and temporary authority become active.
5. All relevant operations are linked to this session.
6. Product events are attributed according to the goal plan version in force.
7. The session closes.
8. A cash discrepancy is detected.
9. Andy analyzes session events and ranks likely causes.
10. The employee's performance ledger is updated according to verified product events.
11. Goal Integrity evaluates quality/persistence signals over time.
12. Management sees both raw performance and quality context.
13. Repeated temporary teller sessions may contribute evidence to role-scope and career analysis, without automatically changing the formal role.

This scenario demonstrates why person, formal role, operational session, authority, evidence, and goal plan must remain distinct entities.

## 29. Relationship to Attention Router

Andy Enterprise should reuse the architectural strengths already present in Attention Router rather than create a separate intelligence island.

Relevant foundations include:

- actor and relationship resolution;
- policy-driven behavior;
- authority and disclosure boundaries;
- controlled autonomy;
- provenance/audit events;
- capability execution;
- conversational context;
- memory with evidence;
- multimodal interaction;
- fail-closed behavior;
- human-in-the-loop control.

The Enterprise module should evolve as additional context, capability, policy, and evidence domains around the same agent architecture.

## 30. Open Architecture Questions

Future design work should resolve at least:

- canonical entities for Organization, Employment, Role, Session, AuthorityGrant, ProcedureVersion, OperationalEvent, GoalPlan, GoalRule, GoalAttribution, CustomerInteractionEvidence, and IntegrityFinding;
- retention classes and privacy boundaries for operational evidence;
- whether operational sessions are owned by Attention Router or referenced from source systems;
- event ingestion contracts from banking/enterprise systems;
- policy boundaries between employee, manager, HR, compliance, audit, and security;
- customer-data minimization requirements;
- how post-sale outcomes are correlated back to original sessions;
- scoring recalculation when a Goal Plan changes retroactively;
- explainability requirements for forecasts and integrity findings;
- how temporary delegation is authorized and terminated;
- how career signals remain advisory rather than automatically dispositive;
- how organizational learning is aggregated without exposing unnecessary employee-level detail.

## 31. Product Positioning

Andy Enterprise should not be positioned as "AI that watches employees" or "a chatbot for company manuals."

The stronger product definition is:

> A contextual enterprise agent that understands who a person is in the organization, what role they are exercising now, what they are authorized to do, what happened during their work, which rules were in force, and how those facts relate to procedures, goals, quality, learning, and management decisions.

The differentiator is not only knowledge retrieval. It is **context + authority + temporal state + evidence + provenance + controlled action**.

## 32. Future GitHub Evolution

This document is intended to become a public-facing evolution/design artifact after the repository's public-release readiness work is complete.

Before public exposure, review examples, terminology, operational assumptions, privacy language, and any potentially sensitive institutional details.

Future updates should append or revise concepts as the Enterprise architecture matures. Significant architectural decisions should later graduate into ADRs or formal contracts only when implementation begins.

---

Last conceptual consolidation: 2026-09-08.