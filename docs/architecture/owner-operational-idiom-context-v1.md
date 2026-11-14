# Contextual owner command interpretation and confirmed idiolect V1

Status: implementation candidate, no production deploy. This work extends the existing authenticated Android command path and the existing User Idiolect, without creating an engine, scheduler, database table, network endpoint or permission.

## Observed gap and intended behavior

While Andy is paused, the owner may write "voltar à vida" as a figurative instruction to resume automatic responses. The direct parser does not recognize this phrase. The existing unconstrained semantic model can return an unresolved meaning, which the client previously routed to general-task pending, despite having a useful persisted state: the owner automation pause.

The new path supplies the model with only one trusted, read-only operational field, PAUSED, ACTIVE or UNKNOWN, obtained from the authenticated owner's current automation-control row. It does not send private message history, contacts, credentials or arbitrary user data as trusted context. The operator text remains untrusted and cannot alter the prompt rules.

When PAUSED and the owner utters an unknown unquoted, nonhypothetical phrase, the candidate builder can propose SET_AUTOMATIC_RESPONSES enabled=true. The model does not execute and does not grant authority. First use requires clarification, such as asking whether the owner intended to activate automatic responses; an affirmative answer is resolved through the original pending-intent and owner-control authorization paths.

Successful explicit confirmation creates the existing USER_CONFIRMED_LANGUAGE fact within the original tenant, owner, channel and conversation boundaries. An exact, nonexpired, unambiguous confirmed expression can be reused without an additional model call when the owner is paused again, but only after fresh authenticated owner and state checks. An idiolect fact is interpretive evidence, never a bearer token or independent grant of power.

## Fail-closed rules

- Canonical pause/resume commands keep the original deterministic fast path and its immediate effect.
- No model invocation is needed for an exact confirmed, valid and scope-matched owner expression.
- Metaphorical first-use commands do not become direct toggle actions from model confidence, even if a model returns MATCHED high.
- Contradictory confirmed meanings do not choose a winner. Negative, quoted, hypothetical, interrogative and multi-line inputs cannot use the implicit activation route.
- When already ACTIVE or when the control state is UNKNOWN, figurative "resume" inferences do not activate anything.
- If the model fails or returns unresolved, the command falls back through the existing nonexecuting general-task path. This increment does not claim every metaphor will be recognized without an available semantic model.
- Existing pending intents retain their ownership, TTL, cancellation, supersession and scope checks. Confirmation invokes the original executor; it does not install a second execution path.
- Idiolect facts remain time limited, scope limited and correctable by explicit subsequent confirmation, as in the existing projection contract.

## Live and historical tracks

Live continuity is unchanged. The Android client command channel still authenticates the device session, resolves the represented owner and persists commands; authorized owner control changes continue to use existing idempotent control changes. No message-ingress history, decision pipeline, TTS, outbox or delivery semantics are changed.

Historical acceleration is not implemented or implied by this increment. Repeated phrases or an adjacent later "retomar" message do not retroactively create confirmed idiolect facts or authorize actions. The confirmed mapping requires a real, explicit clarification resolution.

No new network identities, container attachments, VLANs, ports, addresses or transport authority exist. The change is software-only in the existing API and PostgreSQL contracts.

## Verification and rollout boundary

Synthetic tests cover: paused-state context presented to the candidate model, one-model first-use clarification, explicit confirmation, confirmed-fact persistence, reuse without model, model-asserted action without operational evidence, active-state denial, quoted/negated/interrogative denial, tenant and conversation scoping, expiry and conflicting language facts. No provider call or real command is used during these tests.

The feature depends on the existing owner-control semantic flag and existing model availability. The change must be validated on the exact branch SHA by GitHub CI before integration; no production toggle, message send, credentials, runtime deployment or unpause is required to stage this PR.

Rollback consists of restoring the previous API image. No schema migration or permanent runtime state transformation is part of the code change. Any language facts created under the new flow retain their existing individual expiry and correction rules.
