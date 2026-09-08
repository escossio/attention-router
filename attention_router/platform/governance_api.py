from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.platform.governance import (
    GovernanceDenied,
    GovernanceNotFound,
    create_diagnosis_candidate,
    create_remediation_proposal,
    list_governance_records,
    record_verification_run,
    register_patch_candidate,
    request_promotion,
)
from attention_router.platform.privacy import PrivacyViolation, sanitize_metadata, sanitized_summary


class _StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiagnosisCreate(_StrictPayload):
    tenant_id: str = DEFAULT_TENANT_ID
    finding_id: str
    engine: str = Field(min_length=1, max_length=120)
    source: str = Field(min_length=1, max_length=120)
    hypothesis: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    alternatives: list[dict[str, Any]] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    suspected_component: str | None = Field(default=None, max_length=160)
    provenance: dict[str, Any] = Field(default_factory=dict)
    supersedes_diagnosis_id: str | None = None


class RemediationProposalCreate(_StrictPayload):
    tenant_id: str = DEFAULT_TENANT_ID
    diagnosis_candidate_id: str
    scope: dict[str, Any]
    affected_components: list[str]
    change_summary: str = Field(min_length=1, max_length=1000)
    risk_ids: list[str]
    rollback_requirements: list[str]
    required_tests: list[str]
    required_invariants: list[str]
    required_evidence: list[str]
    author_type: str = Field(min_length=1, max_length=40)
    author_reference: str = Field(min_length=1, max_length=120)
    provenance: dict[str, Any] = Field(default_factory=dict)
    supersedes_proposal_id: str | None = None


class PatchCandidateCreate(_StrictPayload):
    tenant_id: str = DEFAULT_TENANT_ID
    base_sha: str = Field(min_length=7, max_length=128)
    candidate_sha: str = Field(min_length=7, max_length=128)
    branch_reference: str = Field(min_length=1, max_length=240)
    worktree_reference: str = Field(min_length=1, max_length=320)
    artifact_references: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    remediation_proposal_id: str | None = None


class VerificationRunCreate(_StrictPayload):
    tenant_id: str = DEFAULT_TENANT_ID
    patch_candidate_id: str
    observed_candidate_sha: str = Field(min_length=7, max_length=128)
    environment_identity: str = Field(min_length=1, max_length=160)
    test_results: dict[str, Any] = Field(min_length=1)
    differential_results: dict[str, Any] = Field(min_length=1)
    rollback_evidence: list[str] = Field(min_length=1)
    artifact_references: list[str] = Field(min_length=1)
    secret_scan_status: Literal["PASS", "FAIL"]
    assertion_result_ids: list[str] = Field(min_length=1)
    invariant_result_ids: list[str] = Field(min_length=1)
    provenance: dict[str, Any] = Field(min_length=1)


class PromotionRequestCreate(_StrictPayload):
    tenant_id: str = DEFAULT_TENANT_ID
    patch_candidate_id: str
    verification_run_id: str
    required_source_sha: str = Field(min_length=7, max_length=128)
    required_runtime_sha: str | None = Field(default=None, min_length=7, max_length=128)
    required_schema_revision: str = Field(min_length=1, max_length=128)
    evidence_coverage: dict[str, Any] = Field(min_length=1)
    blocking_finding_snapshot: list[str] = Field(default_factory=list)
    risk_disposition: dict[str, Any] = Field(min_length=1)
    rollback_reference: str = Field(min_length=1, max_length=320)
    reason: str = Field(min_length=1, max_length=1000)
    provenance: dict[str, Any] = Field(min_length=1)


class HumanPromotionDecisionCreate(_StrictPayload):
    tenant_id: str = DEFAULT_TENANT_ID
    decision: Literal["APPROVED", "REJECTED", "CANCELLED"]
    reason: str = Field(min_length=1, max_length=1000)


def _record_to_dict(row: Any) -> dict[str, Any]:
    mapper = inspect(row).mapper
    return {attribute.key: getattr(row, attribute.key) for attribute in mapper.column_attrs}


def _safe_metadata(value: dict[str, Any]) -> dict[str, Any]:
    return dict(sanitize_metadata(value).value)


def _safe_list(values: list[Any]) -> list[Any]:
    return list(sanitize_metadata({"items": values}).value["items"])


def _safe_summary(value: str, *, maximum_length: int) -> str:
    result = sanitized_summary(value, maximum_length=maximum_length)
    if result is None:
        raise PrivacyViolation("SUMMARY_REQUIRED", path="summary")
    return result


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, GovernanceNotFound):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, PrivacyViolation):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    detail = str(exc)
    forbidden = (
        "HUMAN_PROMOTION_AUTHORITY_REQUIRED",
        "TENANT_SCOPE_MISMATCH",
        "FORBIDDEN_OPERATIONAL_FIELD",
        "TRUSTED_HUMAN_PROMOTION_CHANNEL_REQUIRED",
    )
    code = status.HTTP_403_FORBIDDEN if detail.startswith(forbidden) else status.HTTP_409_CONFLICT
    raise HTTPException(status_code=code, detail=detail) from exc


def build_governance_router(*, get_session: Any, require_admin: Any) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/platform/governance",
        tags=["platform-governance"],
        dependencies=[Depends(require_admin)],
    )

    @router.get("/records")
    def records(
        tenant_id: str = DEFAULT_TENANT_ID,
        session: Session = Depends(get_session),
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            category: [_record_to_dict(row) for row in rows]
            for category, rows in list_governance_records(session, tenant_id).items()
        }

    @router.post("/diagnoses", status_code=status.HTTP_201_CREATED)
    def create_diagnosis(
        payload: DiagnosisCreate,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        try:
            row = create_diagnosis_candidate(
                session,
                tenant_id=payload.tenant_id,
                finding_id=payload.finding_id,
                engine=_safe_summary(payload.engine, maximum_length=120),
                source=_safe_summary(payload.source, maximum_length=120),
                hypothesis=_safe_summary(payload.hypothesis, maximum_length=1000),
                confidence=payload.confidence,
                supporting_evidence_ids=_safe_list(payload.supporting_evidence_ids),
                contradicting_evidence_ids=_safe_list(payload.contradicting_evidence_ids),
                alternatives=_safe_list(payload.alternatives),
                missing_information=_safe_list(payload.missing_information),
                suspected_component=(
                    _safe_summary(payload.suspected_component, maximum_length=160)
                    if payload.suspected_component
                    else None
                ),
                provenance=_safe_metadata(payload.provenance),
                supersedes_diagnosis_id=payload.supersedes_diagnosis_id,
            )
        except (GovernanceDenied, GovernanceNotFound, PrivacyViolation) as exc:
            _raise_http(exc)
        return _record_to_dict(row)

    @router.post("/remediation-proposals", status_code=status.HTTP_201_CREATED)
    def create_proposal(
        payload: RemediationProposalCreate,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        try:
            row = create_remediation_proposal(
                session,
                tenant_id=payload.tenant_id,
                diagnosis_candidate_id=payload.diagnosis_candidate_id,
                scope=_safe_metadata(payload.scope),
                affected_components=_safe_list(payload.affected_components),
                change_summary=_safe_summary(payload.change_summary, maximum_length=1000),
                risk_ids=_safe_list(payload.risk_ids),
                rollback_requirements=_safe_list(payload.rollback_requirements),
                required_tests=_safe_list(payload.required_tests),
                required_invariants=_safe_list(payload.required_invariants),
                required_evidence=_safe_list(payload.required_evidence),
                author_type=_safe_summary(payload.author_type, maximum_length=40),
                author_reference=_safe_summary(payload.author_reference, maximum_length=120),
                provenance=_safe_metadata(payload.provenance),
                supersedes_proposal_id=payload.supersedes_proposal_id,
            )
        except (GovernanceDenied, GovernanceNotFound, PrivacyViolation) as exc:
            _raise_http(exc)
        return _record_to_dict(row)

    @router.post("/patch-candidates", status_code=status.HTTP_201_CREATED)
    def create_patch_candidate(
        payload: PatchCandidateCreate,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        try:
            row = register_patch_candidate(
                session,
                tenant_id=payload.tenant_id,
                base_sha=_safe_summary(payload.base_sha, maximum_length=128),
                candidate_sha=_safe_summary(payload.candidate_sha, maximum_length=128),
                branch_reference=_safe_summary(payload.branch_reference, maximum_length=240),
                worktree_reference=_safe_summary(payload.worktree_reference, maximum_length=320),
                artifact_references=_safe_list(payload.artifact_references),
                provenance=_safe_metadata(payload.provenance),
                remediation_proposal_id=payload.remediation_proposal_id,
                canonical_worktree=str(Path.cwd()),
            )
        except (GovernanceDenied, GovernanceNotFound, PrivacyViolation) as exc:
            _raise_http(exc)
        return _record_to_dict(row)

    @router.post("/verification-runs", status_code=status.HTTP_201_CREATED)
    def create_verification(
        payload: VerificationRunCreate,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        try:
            row = record_verification_run(
                session,
                tenant_id=payload.tenant_id,
                patch_candidate_id=payload.patch_candidate_id,
                observed_candidate_sha=payload.observed_candidate_sha,
                environment_identity=_safe_summary(payload.environment_identity, maximum_length=160),
                test_results=_safe_metadata(payload.test_results),
                differential_results=_safe_metadata(payload.differential_results),
                rollback_evidence=_safe_list(payload.rollback_evidence),
                artifact_references=_safe_list(payload.artifact_references),
                secret_scan_status=payload.secret_scan_status,
                assertion_result_ids=_safe_list(payload.assertion_result_ids),
                invariant_result_ids=_safe_list(payload.invariant_result_ids),
                provenance=_safe_metadata(payload.provenance),
            )
        except (GovernanceDenied, GovernanceNotFound, PrivacyViolation) as exc:
            _raise_http(exc)
        return _record_to_dict(row)

    @router.post("/promotion-requests", status_code=status.HTTP_201_CREATED)
    def create_promotion_request(
        payload: PromotionRequestCreate,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        try:
            row = request_promotion(
                session,
                tenant_id=payload.tenant_id,
                patch_candidate_id=payload.patch_candidate_id,
                verification_run_id=payload.verification_run_id,
                required_source_sha=payload.required_source_sha,
                required_runtime_sha=payload.required_runtime_sha,
                required_schema_revision=payload.required_schema_revision,
                evidence_coverage=_safe_metadata(payload.evidence_coverage),
                blocking_finding_snapshot=_safe_list(payload.blocking_finding_snapshot),
                risk_disposition=_safe_metadata(payload.risk_disposition),
                rollback_reference=_safe_summary(payload.rollback_reference, maximum_length=320),
                reason=_safe_summary(payload.reason, maximum_length=1000),
                provenance=_safe_metadata(payload.provenance),
            )
        except (GovernanceDenied, GovernanceNotFound, PrivacyViolation) as exc:
            _raise_http(exc)
        return _record_to_dict(row)

    @router.post(
        "/promotion-requests/{promotion_request_id}/decisions",
        status_code=status.HTTP_201_CREATED,
    )
    def create_human_decision(
        promotion_request_id: str,
        payload: HumanPromotionDecisionCreate,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        del promotion_request_id, payload, session
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="TRUSTED_HUMAN_PROMOTION_CHANNEL_REQUIRED",
        )

    return router
