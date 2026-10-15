"""Purely structural grading of a synthetic investigator run.

This is not a causal oracle and does not verify that a model reasoned correctly.
It inspects evidence links, budget accounting, and per-query audit coverage.
"""

from __future__ import annotations

from dataclasses import dataclass

from attention_router.investigator.model import Investigation


@dataclass(frozen=True)
class StructuralAssessment:
    passed: bool
    checks: dict[str, bool]
    causal_proof: str = "NOT_EVALUATED"
    oracle_independence: str = "NOT_PROVABLE_FROM_RESULT"


def assess_replay_structure(
    investigation: Investigation,
    audit_events: list[tuple[str, str, str]],
    *,
    expected_seed_sha256: str,
    max_queries: int,
) -> StructuralAssessment:
    """Check only properties supported by an offline replay artifact."""
    own_events = [
        message for investigation_id, correlation_id, message in audit_events
        if investigation_id == investigation.investigation_id
        and correlation_id == investigation.event.correlation_id
    ]
    requested = [event for event in own_events if ".requested:" in event]
    observed = [event for event in own_events if ".observed:" in event]
    original = investigation.event.original
    decoded = original.decode("utf-8", errors="replace")

    findings = investigation.findings
    evidence = () if findings is None else findings.evidence
    referents = set(range(len(evidence)))
    # Interrupted/blocked sessions legitimately have no final Findings yet.
    if findings is None:
        hypotheses_valid = investigation.status in {"limited", "blocked", "failed"}
        conclusion_valid = hypotheses_valid
    else:
        hypotheses_valid = all(
            set(item.evidence_indexes) <= referents for item in findings.hypotheses
        )
        conclusion_valid = (
            findings.conclusion is None
            or set(findings.conclusion.evidence_indexes) <= referents
        )

    checks = {
        "original_event_digest": investigation.event.digest == expected_seed_sha256,
        "query_budget": 0 <= investigation.query_count <= max_queries,
        "every_counted_query_audited": len(requested) == investigation.query_count,
        "observations_audited": len(observed) == len(investigation.partial_evidence),
        "observations_retained": all(item in investigation.partial_evidence for item in evidence),
        "hypothesis_references_valid": hypotheses_valid,
        "conclusion_references_valid": conclusion_valid,
        "audit_scope": bool(own_events) and own_events[0] == "started",
        "no_literal_event_in_audit": not decoded or decoded not in repr(own_events),
    }
    return StructuralAssessment(passed=all(checks.values()), checks=checks)
