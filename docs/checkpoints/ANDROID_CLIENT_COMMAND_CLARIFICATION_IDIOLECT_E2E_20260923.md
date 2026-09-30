# Android Client Command Clarification + User Idiolect physical proof — 2026-09-23

## Scope

This checkpoint records the first successful physical Android end-to-end proof that free-language Client Command input can cross the existing closed semantic owner-control boundary, request human clarification, execute the confirmed canonical action, persist the confirmed language meaning and reuse that meaning later without repeated clarification.

The proof is intentionally narrow. It records the proven Client Command -> Clarification -> PendingIntent -> User Idiolect path and does not broaden the canonical intent registry.

## Authoritative code

Attention Router:

- PR #176 — `feat: bridge native client commands to intent clarification`
  - merge commit: `a2bd05b178b7780b2300d82c3090a492fa40755c`
  - generalized PendingIntent provenance for native Client Command sources;
  - connected Android Client Command to the existing Clarification and User Idiolect pipeline;
  - introduced migration `0050_client_pending_source`.
- PR #181 — `feat: clarify unresolved client commands before general fallback`
  - merge commit: `6b2aa7325ef57e3e876b62705f5bff725eafbba0`
  - lets semantic `NOT_CONTROL_COMMAND` input receive one candidate-builder pass before `GENERAL_TASK_PENDING` when semantic owner-control is enabled;
  - reuses the existing Clarification, PendingIntent and User Idiolect machinery;
  - preserves general-task fallback when the candidate builder returns no owner-control candidate.

Live backend at proof time:

- runtime head: `6b2aa7325ef57e3e876b62705f5bff725eafbba0`;
- schema revision: `0050_client_pending_source`;
- `OWNER_CONTROL_SEMANTIC_ENABLED=true`;
- Client API health: ready.

Android:

- the physical proof used the signed Android client already installed on the test device with the native Client Command channel;
- the installed product build had previously been produced from the Android line containing PR #50 / merge `2f64c232367b5d2eae5dd6b02c87d9b652b61718`;
- the Android repository later advanced to `977976e8435288d26790a711c4ce654a864e2d57` through session-renewal work; that later session lifecycle frontier is not part of this semantic proof.

## Proven semantic boundary

The architecture proven here is:

`human language -> primary semantic interpretation -> candidate builder when needed -> PendingIntent -> human clarification -> canonical intent -> authority/capability validation -> execution -> USER_CONFIRMED_LANGUAGE -> future Idiolect retrieval`

The AI does not invent executable commands. Human language is mapped into the existing closed semantic registry and materialized only through registered canonical owner-control actions.

## Physical proof

A physical Android device completed the following sequence through the native Client Command UI.

### First learning pass

1. User sent `pare`.
2. Andy executed the existing canonical automatic-response disable action.
3. User sent `voltar a trabalhar`.
4. The primary semantic interpreter did not close the meaning directly.
5. The candidate builder produced the registered meaning:
   - semantic intent: `SET_AUTOMATIC_RESPONSES`;
   - parameters: `{"enabled": true}`;
   - capability: available.
6. Andy rendered a real clarification:
   - `Você quis dizer: ativar as respostas automáticas? Responda "sim" ou "não".`
7. User replied `sim`.
8. The PendingIntent resolved through the native Client Command source.
9. Andy executed canonical action `SET_AUTOMATIC_RESPONSES_ENABLED=true`.
10. The resolved meaning projected a `USER_CONFIRMED_LANGUAGE` fact with predicate `idiolect.pragmatic_mapping`.

Visible result:

`Andy retomada. Respostas automáticas novamente ativas.`

### Reuse pass

1. User sent `pare` again.
2. Andy disabled automatic responses again.
3. User sent the exact same expression: `voltar a trabalhar`.
4. Andy executed the canonical resume action directly.
5. No second clarification was shown.

Visible result:

`Andy retomada. Respostas automáticas novamente ativas.`

This second pass is the physical proof that the explicitly confirmed meaning was retrieved and reused rather than requiring repeated clarification.

## Supporting evidence

PR #181 local focused validation before publication:

- ClientCommandService targeted suite: 9 passed;
- Clarification / semantic candidates / User Idiolect focused suite: 69 passed;
- Ruff: passed;
- `git diff --check`: passed.

PR #181 repository certification completed through the protected branch checks before merge.

The physical proof additionally demonstrated that ordinary unrelated text can still remain outside the owner-control path. Candidate absence continues to preserve `GENERAL_TASK_PENDING` behavior.

## Important negative evidence and boundaries

This checkpoint does **not** claim:

- that `voltar à vida` is currently understood by the real semantic model or candidate builder;
- that every metaphor for resume/pause is understood;
- that `ONE_SHOT_REPLY_DELAY` is executable — that semantic meaning is currently understood in some cases but its capability remains unavailable;
- that Android Client Session lifecycle/network resilience is closed;
- that IPv4/IPv6 transport behavior is resolved;
- that the Android visual-presence/avatar frontier is part of this semantic proof.

The proof also did **not** require:

- adding `voltar a trabalhar` as a hardcoded fast-path alias;
- creating a second executor;
- changing memberships or tenant bindings;
- expanding the closed semantic registry.

## Resume rule

Future work must treat the following as proven and must not rebuild it from scratch:

1. native Android Client Command can enter Intent Clarification;
2. Client Command can create and resolve provider-neutral PendingIntent state;
3. confirmed Client Command language can project into User Idiolect;
4. exact compatible confirmed language can later materialize the canonical meaning without repeated clarification;
5. semantic `NOT_CONTROL_COMMAND` receives a candidate-builder opportunity before general-task fallback when semantic owner-control is enabled;
6. ordinary text with no registered owner-control candidate still falls through to the general task path.

New language coverage should prefer the existing semantic/candidate/Idiolect architecture over growing a hardcoded phrase catalog.

Session lifecycle, dual-stack transport resilience, new canonical capabilities and broader metaphor coverage are separate frontiers.
