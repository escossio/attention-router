from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    AssertionResultRow,
    DiagnosisCandidateRow,
    FindingRow,
    InvariantResultRow,
    PatchCandidateRow,
    PromotionDecisionRow,
    RemediationProposalRow,
    TenantRow,
    VerificationRunRow,
)


class GovernanceDenied(RuntimeError):
    pass


class GovernanceNotFound(KeyError):
    pass


_FORBIDDEN_STRUCTURED_KEYS = {
    "apply",
    "auto_apply",
    "canonical_write",
    "chain_of_thought",
    "deploy",
    "gate_change",
    "open_gate",
    "private_reasoning",
    "runtime_mutation",
    "secret",
}


def _require_tenant(session: Session, tenant_id: str) -> None:
    if session.get(TenantRow, tenant_id) is None:
        raise GovernanceDenied("TENANT_NOT_FOUND")


def _require_same_tenant(row: Any, tenant_id: str, entity: str) -> None:
    if row is None:
        raise GovernanceNotFound(entity)
    if row.tenant_id != tenant_id:
        raise GovernanceDenied(f"TENANT_SCOPE_MISMATCH:{entity}")


def _validate_structured_boundary(value: Any, *, path: str = "root") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _FORBIDDEN_STRUCTURED_KEYS:
                raise GovernanceDenied(f"FORBIDDEN_OPERATIONAL_FIELD:{path}.{key}")
            _validate_structured_boundary(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _validate_structured_boundary(nested, path=f"{path}[{index}]")


def create_diagnosis_candidate(
    session: Session,
    *,
    tenant_id: str,
    finding_id: str,
    engine: str,
    source: str,
    hypothesis: str,
    confidence: float,
    supporting_evidence_ids: list[str] | None = None,
    contradicting_evidence_ids: list[str] | None = None,
    alternatives: list[dict[str, Any]] | None = None,
    missing_information: list[str] | None = None,
    suspected_component: str | None = None,
    provenance: dict[str, Any] | None = None,
    supersedes_diagnosis_id: str | None = None,
) -> DiagnosisCandidateRow:
    """Persist a bounded hypothesis record; it has no mutation execution path."""
    _require_tenant(session, tenant_id)
    finding = session.get(FindingRow, finding_id)
    _require_same_tenant(finding, tenant_id, "finding")
    if not 0 <= confidence <= 1:
        raise GovernanceDenied("CONFIDENCE_OUT_OF_RANGE")
    if not hypothesis.strip():
        raise GovernanceDenied("HYPOTHESIS_REQUIRED")
    structured = {
        "alternatives": alternatives or [],
        "provenance": provenance or {},
    }
    _validate_structured_boundary(structured)
    if supersedes_diagnosis_id:
        previous = session.get(DiagnosisCandidateRow, supersedes_diagnosis_id)
        _require_same_tenant(previous, tenant_id, "superseded_diagnosis")
    row = DiagnosisCandidateRow(
        id=new_id(),
        tenant_id=tenant_id,
        finding_id=finding_id,
        engine=engine,
        source=source,
        hypothesis=hypothesis.strip(),
        confidence=confidence,
        supporting_evidence_ids=supporting_evidence_ids or [],
        contradicting_evidence_ids=contradicting_evidence_ids or [],
        alternatives=alternatives or [],
        missing_information=missing_information or [],
        suspected_component=suspected_component,
        status="CANDIDATE",
        provenance=provenance or {},
        created_at=now_utc(),
        supersedes_diagnosis_id=supersedes_diagnosis_id,
    )
    session.add(row)
    session.flush()
    return row


def create_remediation_proposal(
    session: Session,
    *,
    tenant_id: str,
    diagnosis_candidate_id: str,
    scope: dict[str, Any],
    affected_components: list[str],
    change_summary: str,
    risk_ids: list[str],
    rollback_requirements: list[str],
    required_tests: list[str],
    required_invariants: list[str],
    required_evidence: list[str],
    author_type: str,
    author_reference: str,
    provenance: dict[str, Any] | None = None,
    supersedes_proposal_id: str | None = None,
) -> RemediationProposalRow:
    """Create a proposal record only; no apply/deploy callable is exposed."""
    _require_tenant(session, tenant_id)
    diagnosis = session.get(DiagnosisCandidateRow, diagnosis_candidate_id)
    _require_same_tenant(diagnosis, tenant_id, "diagnosis")
    _validate_structured_boundary({"scope": scope, "provenance": provenance or {}})
    if not change_summary.strip() or not rollback_requirements or not required_tests:
        raise GovernanceDenied("PROPOSAL_SAFETY_CONTRACT_INCOMPLETE")
    if supersedes_proposal_id:
        previous = session.get(RemediationProposalRow, supersedes_proposal_id)
        _require_same_tenant(previous, tenant_id, "superseded_proposal")
    row = RemediationProposalRow(
        id=new_id(),
        tenant_id=tenant_id,
        diagnosis_candidate_id=diagnosis_candidate_id,
        scope=scope,
        affected_components=affected_components,
        change_summary=change_summary.strip(),
        risk_ids=risk_ids,
        rollback_requirements=rollback_requirements,
        required_tests=required_tests,
        required_invariants=required_invariants,
        required_evidence=required_evidence,
        status="PROPOSED",
        author_type=author_type,
        author_reference=author_reference,
        provenance=provenance or {},
        created_at=now_utc(),
        supersedes_proposal_id=supersedes_proposal_id,
    )
    session.add(row)
    session.flush()
    return row


def register_patch_candidate(
    session: Session,
    *,
    tenant_id: str,
    base_sha: str,
    candidate_sha: str,
    branch_reference: str,
    worktree_reference: str,
    artifact_references: list[str] | None = None,
    provenance: dict[str, Any] | None = None,
    remediation_proposal_id: str | None = None,
    canonical_worktree: str | None = None,
) -> PatchCandidateRow:
    _require_tenant(session, tenant_id)
    if base_sha == candidate_sha or not base_sha or not candidate_sha:
        raise GovernanceDenied("PATCH_CANDIDATE_SHA_INVALID")
    if canonical_worktree and Path(worktree_reference).resolve() == Path(canonical_worktree).resolve():
        raise GovernanceDenied("PATCH_VERIFICATION_WORKTREE_NOT_ISOLATED")
    if remediation_proposal_id:
        proposal = session.get(RemediationProposalRow, remediation_proposal_id)
        _require_same_tenant(proposal, tenant_id, "remediation_proposal")
    _validate_structured_boundary(provenance or {})
    row = PatchCandidateRow(
        id=new_id(),
        tenant_id=tenant_id,
        remediation_proposal_id=remediation_proposal_id,
        base_sha=base_sha,
        candidate_sha=candidate_sha,
        branch_reference=branch_reference,
        worktree_reference=worktree_reference,
        artifact_references=artifact_references or [],
        provenance=provenance or {},
        status="IMPLEMENTED_CANDIDATE",
        created_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row


def record_verification_run(
    session: Session,
    *,
    tenant_id: str,
    patch_candidate_id: str,
    observed_candidate_sha: str,
    environment_identity: str,
    test_results: dict[str, Any],
    differential_results: dict[str, Any],
    rollback_evidence: list[str],
    artifact_references: list[str],
    secret_scan_status: str,
    assertion_result_ids: list[str] | None = None,
    invariant_result_ids: list[str] | None = None,
    provenance: dict[str, Any] | None = None,
) -> VerificationRunRow:
    candidate = session.get(PatchCandidateRow, patch_candidate_id)
    _require_same_tenant(candidate, tenant_id, "patch_candidate")
    if candidate.candidate_sha != observed_candidate_sha:
        raise GovernanceDenied("CANDIDATE_SHA_MISMATCH_NEW_VERIFICATION_REQUIRED")
    _validate_structured_boundary(
        {"tests": test_results, "differential": differential_results, "provenance": provenance or {}}
    )
    required_test_fields = {"passed", "failed"}
    required_differential_fields = {"baseline_failures", "final_failures", "new_failures"}
    if not test_results or not required_test_fields.issubset(test_results):
        raise GovernanceDenied("VERIFICATION_TEST_EVIDENCE_INCOMPLETE")
    if not differential_results or not required_differential_fields.issubset(
        differential_results
    ):
        raise GovernanceDenied("VERIFICATION_DIFFERENTIAL_EVIDENCE_INCOMPLETE")
    numeric_values = {
        "passed": test_results["passed"],
        "failed": test_results["failed"],
        "baseline_failures": differential_results["baseline_failures"],
        "final_failures": differential_results["final_failures"],
        "new_failures": differential_results["new_failures"],
    }
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in numeric_values.values()
    ):
        raise GovernanceDenied("VERIFICATION_RESULT_COUNTS_INVALID")
    if numeric_values["passed"] + numeric_values["failed"] == 0:
        raise GovernanceDenied("VERIFICATION_NO_TESTS_EXECUTED")
    required_reference_sets = {
        "rollback": rollback_evidence,
        "artifacts": artifact_references,
        "assertions": assertion_result_ids or [],
        "invariants": invariant_result_ids or [],
    }
    if any(
        not references or any(not str(reference).strip() for reference in references)
        for references in required_reference_sets.values()
    ):
        raise GovernanceDenied("VERIFICATION_REQUIRED_EVIDENCE_MISSING")
    if not provenance:
        raise GovernanceDenied("VERIFICATION_PROVENANCE_MISSING")
    if secret_scan_status not in {"PASS", "FAIL"}:
        raise GovernanceDenied("VERIFICATION_SECRET_SCAN_STATUS_INVALID")
    assertions = _validated_assertion_evidence(
        session,
        tenant_id=tenant_id,
        result_ids=assertion_result_ids or [],
    )
    invariants = _validated_invariant_evidence(
        session,
        tenant_id=tenant_id,
        result_ids=invariant_result_ids or [],
    )
    if any(row.provenance.get("candidate_sha") != observed_candidate_sha for row in assertions):
        raise GovernanceDenied("ASSERTION_EVIDENCE_CANDIDATE_SHA_MISMATCH")
    if any(row.provenance.get("candidate_sha") != observed_candidate_sha for row in invariants):
        raise GovernanceDenied("INVARIANT_EVIDENCE_CANDIDATE_SHA_MISMATCH")
    if candidate.remediation_proposal_id:
        proposal = session.get(RemediationProposalRow, candidate.remediation_proposal_id)
        _require_same_tenant(proposal, tenant_id, "remediation_proposal")
        executed_test_ids = test_results.get("executed_test_ids")
        evidence_classes = test_results.get("evidence_classes")
        if not isinstance(executed_test_ids, list) or not isinstance(evidence_classes, list):
            raise GovernanceDenied("VERIFICATION_PROPOSAL_COVERAGE_MISSING")
        if not set(proposal.required_tests).issubset(set(executed_test_ids)):
            raise GovernanceDenied("VERIFICATION_REQUIRED_TESTS_NOT_COVERED")
        if not set(proposal.required_evidence).issubset(set(evidence_classes)):
            raise GovernanceDenied("VERIFICATION_REQUIRED_EVIDENCE_NOT_COVERED")
        evaluated_invariants = {row.invariant_id for row in invariants}
        if not set(proposal.required_invariants).issubset(evaluated_invariants):
            raise GovernanceDenied("VERIFICATION_REQUIRED_INVARIANTS_NOT_COVERED")
    failures = numeric_values["failed"]
    new_failures = numeric_values["new_failures"]
    passed = failures == 0 and new_failures == 0 and secret_scan_status == "PASS"
    stamp = now_utc()
    row = VerificationRunRow(
        id=new_id(),
        tenant_id=tenant_id,
        patch_candidate_id=patch_candidate_id,
        candidate_sha_snapshot=observed_candidate_sha,
        environment_identity=environment_identity,
        test_results=test_results,
        assertion_result_ids=[row.id for row in assertions],
        invariant_result_ids=[row.id for row in invariants],
        differential_results=differential_results,
        rollback_evidence=rollback_evidence,
        artifact_references=artifact_references,
        secret_scan_status=secret_scan_status,
        result="PATCH_VERIFIED" if passed else "FAILED",
        provenance=provenance or {},
        started_at=stamp,
        completed_at=stamp,
        created_at=stamp,
    )
    session.add(row)
    candidate.status = "PATCH_VERIFIED" if passed else "VERIFICATION_FAILED"
    session.flush()
    return row


def _validated_assertion_evidence(
    session: Session,
    *,
    tenant_id: str,
    result_ids: list[str],
) -> list[AssertionResultRow]:
    if len(set(result_ids)) != len(result_ids):
        raise GovernanceDenied("DUPLICATE_ASSERTION_RESULT_REFERENCE")
    rows = list(
        session.scalars(
            select(AssertionResultRow)
            .where(
                AssertionResultRow.tenant_id == tenant_id,
                AssertionResultRow.id.in_(result_ids),
            )
            .with_for_update()
        ).all()
    )
    if len(rows) != len(result_ids):
        raise GovernanceDenied("ASSERTION_RESULT_MISSING_OR_CROSS_TENANT")
    if any(row.result != "PASS" for row in rows):
        raise GovernanceDenied("ASSERTION_RESULT_NOT_PASS")
    if any(not row.evidence_reference_ids or not row.provenance for row in rows):
        raise GovernanceDenied("ASSERTION_RESULT_EVIDENCE_INCOMPLETE")
    return rows


def _validated_invariant_evidence(
    session: Session,
    *,
    tenant_id: str,
    result_ids: list[str],
) -> list[InvariantResultRow]:
    if len(set(result_ids)) != len(result_ids):
        raise GovernanceDenied("DUPLICATE_INVARIANT_RESULT_REFERENCE")
    rows = list(
        session.scalars(
            select(InvariantResultRow)
            .where(
                InvariantResultRow.tenant_id == tenant_id,
                InvariantResultRow.id.in_(result_ids),
            )
            .with_for_update()
        ).all()
    )
    if len(rows) != len(result_ids):
        raise GovernanceDenied("INVARIANT_RESULT_MISSING_OR_CROSS_TENANT")
    if any(row.result != "PASS" for row in rows):
        raise GovernanceDenied("INVARIANT_RESULT_NOT_PASS")
    if any(not row.evidence_reference_ids or not row.provenance for row in rows):
        raise GovernanceDenied("INVARIANT_RESULT_EVIDENCE_INCOMPLETE")
    return rows


def request_promotion(
    session: Session,
    *,
    tenant_id: str,
    patch_candidate_id: str,
    verification_run_id: str,
    required_source_sha: str,
    required_schema_revision: str,
    evidence_coverage: dict[str, Any],
    blocking_finding_snapshot: list[str],
    risk_disposition: dict[str, Any],
    rollback_reference: str,
    reason: str,
    provenance: dict[str, Any] | None = None,
    required_runtime_sha: str | None = None,
) -> PromotionDecisionRow:
    candidate = session.get(PatchCandidateRow, patch_candidate_id)
    verification = session.get(VerificationRunRow, verification_run_id)
    _require_same_tenant(candidate, tenant_id, "patch_candidate")
    _require_same_tenant(verification, tenant_id, "verification_run")
    if verification.patch_candidate_id != candidate.id:
        raise GovernanceDenied("VERIFICATION_CANDIDATE_MISMATCH")
    if verification.candidate_sha_snapshot != candidate.candidate_sha:
        raise GovernanceDenied("VERIFICATION_SHA_STALE")
    if verification.result != "PATCH_VERIFIED":
        raise GovernanceDenied("PATCH_NOT_VERIFIED")
    if required_source_sha != candidate.candidate_sha:
        raise GovernanceDenied("PROMOTION_SOURCE_CANDIDATE_MISMATCH")
    if not required_schema_revision.strip() or not rollback_reference.strip() or not reason.strip():
        raise GovernanceDenied("PROMOTION_REQUIRED_REFERENCE_MISSING")
    if not evidence_coverage or not risk_disposition or not provenance:
        raise GovernanceDenied("PROMOTION_EVIDENCE_OR_PROVENANCE_MISSING")
    blocking_findings = session.scalars(
        select(FindingRow)
        .where(
            FindingRow.tenant_id == tenant_id,
            FindingRow.status.not_in(("RESOLVED", "EXPECTED")),
            or_(
                FindingRow.category == "BLOCKER",
                FindingRow.severity.in_(("CRITICAL", "HIGH")),
            ),
        )
        .order_by(FindingRow.id)
        .with_for_update()
    ).all()
    current_blocking_ids = [row.id for row in blocking_findings]
    provided_blocking_ids = sorted(set(blocking_finding_snapshot))
    if provided_blocking_ids != current_blocking_ids or len(provided_blocking_ids) != len(
        blocking_finding_snapshot
    ):
        raise GovernanceDenied("BLOCKING_FINDING_SNAPSHOT_MISMATCH")
    if current_blocking_ids:
        raise GovernanceDenied("BLOCKING_FINDINGS_PRESENT")
    _validate_structured_boundary({"evidence": evidence_coverage, "risk": risk_disposition})
    row = PromotionDecisionRow(
        id=new_id(),
        tenant_id=tenant_id,
        patch_candidate_id=patch_candidate_id,
        verification_run_id=verification_run_id,
        promotion_request_id=None,
        decision="PROMOTION_REQUIRED",
        authority_type="SYSTEM_RECORD",
        authority_reference=None,
        evidence_coverage=evidence_coverage,
        blocking_finding_snapshot=blocking_finding_snapshot,
        risk_disposition=risk_disposition,
        rollback_reference=rollback_reference,
        reason=reason,
        decided_at=None,
        required_source_sha=required_source_sha,
        required_runtime_sha=required_runtime_sha,
        required_schema_revision=required_schema_revision,
        provenance=provenance or {},
        created_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row


def record_human_promotion_decision(
    session: Session,
    *,
    tenant_id: str,
    promotion_request_id: str,
    decision: str,
    authority_type: str,
    authority_reference: str,
    reason: str,
    authority_verifier: Callable[[str, str], bool] | None = None,
) -> PromotionDecisionRow:
    request = session.scalar(
        select(PromotionDecisionRow)
        .where(PromotionDecisionRow.id == promotion_request_id)
        .with_for_update()
    )
    _require_same_tenant(request, tenant_id, "promotion_request")
    if request.decision != "PROMOTION_REQUIRED":
        raise GovernanceDenied("PROMOTION_REQUEST_NOT_PENDING")
    if authority_type not in {"HUMAN_OWNER", "HUMAN_OPERATOR"}:
        raise GovernanceDenied("HUMAN_PROMOTION_AUTHORITY_REQUIRED")
    if authority_verifier is None or not authority_verifier(authority_type, authority_reference):
        raise GovernanceDenied("TRUSTED_HUMAN_PROMOTION_CHANNEL_REQUIRED")
    if decision not in {"APPROVED", "REJECTED", "CANCELLED"}:
        raise GovernanceDenied("INVALID_PROMOTION_DECISION")
    terminal_decisions = session.scalars(
        select(PromotionDecisionRow).where(
            PromotionDecisionRow.tenant_id == tenant_id,
            PromotionDecisionRow.promotion_request_id == request.id,
        )
        .with_for_update()
    ).all()
    if terminal_decisions:
        raise GovernanceDenied("PROMOTION_REQUEST_ALREADY_DECIDED")
    row = PromotionDecisionRow(
        id=new_id(),
        tenant_id=tenant_id,
        patch_candidate_id=request.patch_candidate_id,
        verification_run_id=request.verification_run_id,
        promotion_request_id=request.id,
        decision=decision,
        authority_type=authority_type,
        authority_reference=authority_reference,
        evidence_coverage=request.evidence_coverage,
        blocking_finding_snapshot=request.blocking_finding_snapshot,
        risk_disposition=request.risk_disposition,
        rollback_reference=request.rollback_reference,
        reason=reason,
        decided_at=now_utc(),
        required_source_sha=request.required_source_sha,
        required_runtime_sha=request.required_runtime_sha,
        required_schema_revision=request.required_schema_revision,
        provenance=request.provenance,
        created_at=now_utc(),
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        raise GovernanceDenied("PROMOTION_REQUEST_ALREADY_DECIDED") from exc
    return row


def promotion_state(
    decisions: Iterable[PromotionDecisionRow],
    *,
    applied_evidence: bool = False,
    runtime_confirmed_evidence: bool = False,
) -> str:
    ordered = sorted(decisions, key=lambda row: (row.created_at, row.id))
    requests = [row for row in ordered if row.decision == "PROMOTION_REQUIRED"]
    terminal_decisions = [
        row for row in ordered if row.decision in {"APPROVED", "REJECTED", "CANCELLED"}
    ]
    if not requests:
        if terminal_decisions:
            raise GovernanceDenied("PROMOTION_TERMINAL_WITHOUT_REQUEST")
        state = "PATCH_VERIFIED"
    else:
        current_request = requests[-1]
        current_terminals = [
            row
            for row in terminal_decisions
            if row.promotion_request_id == current_request.id
        ]
        if len(current_terminals) > 1:
            raise GovernanceDenied("PROMOTION_REQUEST_TERMINAL_CONFLICT")
        state = current_terminals[0].decision if current_terminals else "PROMOTION_REQUIRED"
    if applied_evidence:
        if state != "APPROVED":
            raise GovernanceDenied("APPLICATION_WITHOUT_HUMAN_APPROVAL")
        state = "APPLIED"
    if runtime_confirmed_evidence:
        if state != "APPLIED":
            raise GovernanceDenied("RUNTIME_CONFIRMATION_WITHOUT_APPLICATION")
        state = "RUNTIME_CONFIRMED"
    return state


def list_governance_records(session: Session, tenant_id: str) -> dict[str, list[Any]]:
    return {
        "diagnoses": list(
            session.scalars(
                select(DiagnosisCandidateRow).where(DiagnosisCandidateRow.tenant_id == tenant_id)
            ).all()
        ),
        "proposals": list(
            session.scalars(
                select(RemediationProposalRow).where(RemediationProposalRow.tenant_id == tenant_id)
            ).all()
        ),
        "candidates": list(
            session.scalars(
                select(PatchCandidateRow).where(PatchCandidateRow.tenant_id == tenant_id)
            ).all()
        ),
        "verifications": list(
            session.scalars(
                select(VerificationRunRow).where(VerificationRunRow.tenant_id == tenant_id)
            ).all()
        ),
        "promotion_decisions": list(
            session.scalars(
                select(PromotionDecisionRow).where(PromotionDecisionRow.tenant_id == tenant_id)
            ).all()
        ),
    }
