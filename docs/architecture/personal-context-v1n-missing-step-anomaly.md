# Personal Context V1N — Governed Missing-Step Anomalies

Status: implementation candidate

Related:
- #84 Personal Context V1
- V1M governed event-sequence hypotheses
- V1H governed owner correction

## Purpose

Detect bounded anomalies when a learned two-step routine is interrupted.

Target:

`active EVENT_SEQUENCE A -> B`
+
`new A occurs after the learned sequence evidence`
+
`the expected B window closes without B`
→
`MISSING_EXPECTED_STEP anomaly hypothesis`

V1N treats absence as an inference, never as a fact.

## Evidence threshold

V1N evaluates only active sequence claims that are already well supported:

- source pattern type = EVENT_SEQUENCE;
- source evidence class = INFERRED;
- source status = HYPOTHESIS;
- source confidence >= 0.80;
- source support ratio >= 0.75;
- source occurrence count >= 3;
- source claim is ACTIVE, non-secret and unexpired.

A single newly observed A is therefore not enough to invent a routine. The
routine must already have been learned by V1M.

## Expected window

The expected A→B gap comes from the source sequence median.

The anomaly window closes at:

`A occurred_at + median_gap + bounded tolerance`

Tolerance uses the same explainable bounds as V1M:

- at least 15 minutes;
- at most 90 minutes;
- otherwise 25% of the expected gap.

No anomaly is emitted while the window is still open.

## Missing-step semantics

A candidate anomaly is created only when:

- A occurred strictly after the source sequence's last learned occurrence;
- the expected window has closed;
- no matching B exists inside that A→B window;
- all evidence is tenant/person scoped and non-secret.

The anomaly has:

- anomaly_type = MISSING_EXPECTED_STEP;
- evidence_class = INFERRED;
- hypothesis_status = HYPOTHESIS;
- source_sequence_claim_id;
- source_hypothesis_id;
- exact first-event TimelineEvent ID;
- expected B event/signature;
- expected gap;
- closed-window timestamp;
- source sequence confidence/support/occurrence count;
- 7-day expiry;
- grants_authority = false;
- recommendation_ready = false.

## One anomaly does not rewrite the routine

V1N persists anomaly knowledge as a separate MemoryClaim:

- predicate = `context.pattern.sequence_anomaly`;
- source_quality = `DERIVED_PATTERN`.

The source `context.pattern.event_sequence` claim is not superseded, weakened,
or mutated merely because one expected step was absent.

This directly preserves the invariant that one anomalous occurrence must not
rewrite a stable routine.

## Revision when late evidence arrives

Absence-based inference must be revisable.

If a TimelineEvent for the expected B arrives later in ingestion but its
`occurred_at` falls inside the original expected window, V1N supersedes the
active anomaly with reason:

`EXPECTED_STEP_EVIDENCE_ARRIVED`

The historical anomaly record remains visible as superseded evidence.

If the source sequence itself becomes inactive, the anomaly is also superseded
with:

`SOURCE_SEQUENCE_INACTIVE`

The source routine is never revived or modified by anomaly reconciliation.

## Race safety

Detection and persistence are separate.

Before persistence, V1N revalidates:

- the source sequence is still active and current;
- the first event is still valid evidence;
- the expected window has closed;
- no B has appeared inside the window.

If B arrives between detection and persistence, persistence fails closed with
`ANOMALY_EXPECTED_STEP_PRESENT`.

## Runtime

The existing V1G Personal Context cycle now:

1. detects/persists temporal recurrence;
2. detects/persists event sequences;
3. reconciles existing missing-step anomalies;
4. detects/persists new missing-step anomalies;
5. builds only the already-approved recommendation types.

V1N adds no new runtime flag and inherits the existing default-off
`PERSONAL_CONTEXT_RUNTIME_ENABLED` boundary.

## Non-actionability

V1N adds no recommendation mapping for sequence anomalies.

Therefore an anomaly cannot directly create:

- proactive recommendation;
- ExecutionIntent;
- ReminderRow;
- OutboxMessageRow;
- provider call;
- FactRow.

A future anomaly-to-capability mapping must be explicit and separately
governed.

## Proof

Tests cover:

- closed expected window without B creates one anomaly candidate;
- still-open window creates no anomaly;
- B inside the expected window blocks anomaly creation;
- persistence is idempotent;
- anomaly persistence creates no FactRow;
- one anomaly leaves the source sequence ACTIVE and unchanged;
- B arriving between detection and persistence fails closed;
- late-ingested B inside the original window supersedes the anomaly;
- inactive source sequence supersedes the anomaly;
- runtime persists anomaly knowledge without recommendation or outbox.

## Runtime state

No migration.

No deploy.

No feature flag is enabled by this change.
