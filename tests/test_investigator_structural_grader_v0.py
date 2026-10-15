"""Pure structural-grader tests; human/causal grading stays separate."""

from attention_router.investigator.model import (
    Conclusion, Evidence, Findings, Hypothesis, Investigation, Query, SourceEvent,
)
from evals.investigator_v0.structural_grader import assess_replay_structure


def _sample():
    event = SourceEvent("synthetic", b"synthetic-alert-only", "text/plain", "synthetic-tenant")
    query = Query("zabbix", "read_event")
    evidence = Evidence(query, {"severity": "warning"}, "synthetic:zabbix:read_event")
    findings = Findings(
        (evidence,),
        (Hypothesis("unknown cause", (0,), "insufficient evidence"),),
        Conclusion("Observation only", (0,), classification="INDICATIVE"),
    )
    result = Investigation("synthetic-investigation", event, findings, "completed", 1,
                           partial_evidence=(evidence,))
    audit = [
        (result.investigation_id, event.correlation_id, "started"),
        (result.investigation_id, event.correlation_id, "query.1.requested:fingerprint"),
        (result.investigation_id, event.correlation_id, "query.1.observed:fingerprint"),
        (result.investigation_id, event.correlation_id, "completed"),
    ]
    return result, audit


def test_structural_assessment_does_not_claim_causality_or_oracle_isolation():
    result, audit = _sample()
    grade = assess_replay_structure(result, audit,
                                    expected_seed_sha256=result.event.digest, max_queries=3)
    assert grade.passed
    assert all(grade.checks.values())
    assert grade.causal_proof == "NOT_EVALUATED"
    assert grade.oracle_independence == "NOT_PROVABLE_FROM_RESULT"


def test_missing_query_audit_fails_without_inventing_causal_result():
    result, audit = _sample()
    grade = assess_replay_structure(result, audit[:1] + audit[2:],
                                    expected_seed_sha256=result.event.digest, max_queries=3)
    assert not grade.passed
    assert not grade.checks["every_counted_query_audited"]
    assert grade.causal_proof == "NOT_EVALUATED"


def test_wrong_seed_hash_or_budget_fails_independently():
    result, audit = _sample()
    grade = assess_replay_structure(result, audit, expected_seed_sha256="0" * 64, max_queries=0)
    assert not grade.passed
    assert not grade.checks["original_event_digest"]
    assert not grade.checks["query_budget"]


def test_invalid_conclusion_reference_cannot_pass_structural_gate():
    result, audit = _sample()
    invalid = Investigation(
        result.investigation_id, result.event,
        Findings(
            result.findings.evidence,
            result.findings.hypotheses,
            Conclusion("fabricated index", (7,), classification="PROVEN"),
        ),
        "completed", 1, partial_evidence=result.partial_evidence,
    )
    grade = assess_replay_structure(
        invalid, audit, expected_seed_sha256=result.event.digest, max_queries=3,
    )
    assert not grade.passed
    assert not grade.checks["conclusion_references_valid"]
    assert grade.causal_proof == "NOT_EVALUATED"
