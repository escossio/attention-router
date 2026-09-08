from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.platform.findings import (
    FindingCandidate,
    FindingCategory,
    FindingSeverity,
    record_finding,
    resolve_finding,
)
from attention_router.platform.assertions import (
    AssertionDefinition,
    append_assertion_result,
    evaluate_predicate,
)
from attention_router.platform.governance import (
    GovernanceDenied,
    promotion_state,
    record_human_promotion_decision,
    record_verification_run,
)
from attention_router.platform.invariants import (
    InvariantOutcome,
    InvariantRegistry,
    append_invariant_result,
)
from attention_router.platform.scenarios import (
    create_scenario_run,
    load_manifest_file,
    register_manifest_version,
)
from attention_router.infrastructure.models import PatchCandidateRow
from attention_router.web.app import app, get_session


@pytest.fixture()
def platform_client(session, monkeypatch):
    session.execute(text("create table alembic_version (version_num varchar(128) not null)"))
    session.execute(
        text("insert into alembic_version (version_num) values ('0019_platform_evolution_wave_d')")
    )
    session.commit()

    def override_session():
        yield session

    monkeypatch.setattr("attention_router.web.app.settings.admin_auth_enabled", False)
    app.dependency_overrides[get_session] = override_session
    try:
        yield TestClient(app), session
    finally:
        app.dependency_overrides.clear()


def _governance_evidence(session):
    manifest_path = Path("config/platform/scenarios/SCN-PE-001.yaml")
    manifest = load_manifest_file(manifest_path)
    version = register_manifest_version(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        manifest=manifest,
        manifest_source_path=str(manifest_path),
        source_sha="a" * 40,
    )
    run = create_scenario_run(
        session,
        run_id="governance-evidence-run",
        tenant_id=DEFAULT_TENANT_ID,
        scenario_version_id=version.id,
        synthetic_actor_binding_id=None,
        root_correlation_id="governance-evidence-correlation",
        source_sha="a" * 40,
        runtime_sha="a" * 40,
        schema_revision="0019_platform_evolution_wave_d",
        driver_revision=None,
        readiness_result_id=None,
        effect_budget_id=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    assertion = append_assertion_result(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        scenario_run_id=run.id,
        scenario_step_run_id=None,
        evaluated=evaluate_predicate(
            AssertionDefinition(
                assertion_id="ACC-PE016-001",
                version=1,
                assertion_type="PROVENANCE",
                expected_property="candidate identity is exact",
                evaluator="deterministic-test",
            ),
            observed=True,
            predicate=lambda value: value is True,
            evidence_refs=("evidence:candidate-sha",),
        ),
        attempt=1,
        provenance={"candidate_sha": "b" * 40},
    )
    invariant = append_invariant_result(
        session,
        registry=InvariantRegistry(),
        tenant_id=DEFAULT_TENANT_ID,
        scenario_run_id=run.id,
        scenario_step_run_id=None,
        invariant_id="INV-PE-REMED-002",
        outcome=InvariantOutcome.PASS,
        attempt=1,
        reason_code="CANDIDATE_IDENTITY_MATCHED",
        evidence_refs=("evidence:candidate-sha",),
        provenance={"candidate_sha": "b" * 40},
    )
    session.commit()
    return assertion, invariant


def test_operations_snapshot_and_dashboard_are_read_only(platform_client):
    client, _ = platform_client

    snapshot = client.get("/api/v1/admin/platform/operations/snapshot")
    assert snapshot.status_code == 200
    assert snapshot.json()["tenant_id"] == DEFAULT_TENANT_ID
    assert snapshot.json()["runtime_provenance"]["schema_revision"] == (
        "0019_platform_evolution_wave_d"
    )

    dashboard = client.get("/ops/platform")
    assert dashboard.status_code == 200
    assert "Platform Operations / Read-only V1" in dashboard.text
    assert "This surface cannot send" in dashboard.text

    assert client.post("/ops/platform").status_code == 405
    assert client.post("/ops/platform/send").status_code == 404
    assert client.post("/ops/platform/gates").status_code == 404
    assert client.post("/api/v1/admin/platform/operations/snapshot").status_code == 405


def test_platform_routes_require_admin_credentials(monkeypatch):
    monkeypatch.setattr("attention_router.web.app.settings.admin_auth_enabled", True)
    assert TestClient(app).get("/ops/platform").status_code == 401
    assert (
        TestClient(app).get("/api/v1/admin/platform/governance/records").status_code
        == 401
    )


def test_governance_records_do_not_execute_or_self_promote(platform_client):
    client, session = platform_client
    finding = record_finding(
        session,
        FindingCandidate(
            tenant_id=DEFAULT_TENANT_ID,
            category=FindingCategory.ANOMALY,
            severity=FindingSeverity.MEDIUM,
            title="Synthetic governance finding",
            summary="Bounded finding used by the governance API contract test.",
            component_key="platform.test",
            reason_code="SYNTHETIC_TEST_FINDING",
        ),
    )
    session.commit()

    diagnosis = client.post(
        "/api/v1/admin/platform/governance/diagnoses",
        json={
            "finding_id": finding.finding_id,
            "engine": "deterministic-test",
            "source": "finding-evidence",
            "hypothesis": "The candidate contract needs isolated verification.",
            "confidence": 0.8,
            "alternatives": [{"summary": "A pre-existing baseline may explain it."}],
            "missing_information": ["candidate test report"],
            "provenance": {"source_revision": "source-a"},
        },
    )
    assert diagnosis.status_code == 201
    diagnosis_id = diagnosis.json()["id"]
    assert diagnosis.json()["status"] == "CANDIDATE"

    forbidden_diagnosis = client.post(
        "/api/v1/admin/platform/governance/diagnoses",
        json={
            "finding_id": finding.finding_id,
            "engine": "deterministic-test",
            "source": "finding-evidence",
            "hypothesis": "This record must not mutate runtime.",
            "confidence": 0.5,
            "provenance": {"runtime_mutation": True},
        },
    )
    assert forbidden_diagnosis.status_code == 403

    proposal = client.post(
        "/api/v1/admin/platform/governance/remediation-proposals",
        json={
            "diagnosis_candidate_id": diagnosis_id,
            "scope": {"modules": ["attention_router.platform"]},
            "affected_components": ["platform-api"],
            "change_summary": "Verify a bounded candidate in an isolated worktree.",
            "risk_ids": ["RPE-023"],
            "rollback_requirements": ["retain additive evidence"],
            "required_tests": ["focused contract tests"],
            "required_invariants": ["INV-PE-REMED-002"],
            "required_evidence": ["TEST_REPORT"],
            "author_type": "AI_PROPOSAL",
            "author_reference": "test-agent",
            "provenance": {"source_revision": "source-a"},
        },
    )
    assert proposal.status_code == 201
    assert proposal.json()["status"] == "PROPOSED"
    proposal_id = proposal.json()["id"]

    candidate_sha = "b" * 40
    candidate = client.post(
        "/api/v1/admin/platform/governance/patch-candidates",
        json={
            "base_sha": "a" * 40,
            "candidate_sha": candidate_sha,
            "branch_reference": "candidate/platform-test",
            "worktree_reference": "/tmp/attention-router-platform-candidate",
            "artifact_references": ["artifact:test-report"],
            "provenance": {"source_revision": candidate_sha},
            "remediation_proposal_id": proposal_id,
        },
    )
    assert candidate.status_code == 201
    candidate_id = candidate.json()["id"]

    empty_verification = client.post(
        "/api/v1/admin/platform/governance/verification-runs",
        json={
            "patch_candidate_id": candidate_id,
            "observed_candidate_sha": candidate_sha,
            "environment_identity": "isolated-test-worktree",
            "test_results": {},
            "differential_results": {},
            "rollback_evidence": [],
            "artifact_references": [],
            "secret_scan_status": "PASS",
            "assertion_result_ids": [],
            "invariant_result_ids": [],
            "provenance": {},
        },
    )
    assert empty_verification.status_code == 422

    with pytest.raises(GovernanceDenied, match="VERIFICATION_TEST_EVIDENCE_INCOMPLETE"):
        record_verification_run(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            patch_candidate_id=candidate_id,
            observed_candidate_sha=candidate_sha,
            environment_identity="isolated-test-worktree",
            test_results={},
            differential_results={},
            rollback_evidence=[],
            artifact_references=[],
            secret_scan_status="PASS",
        )
    assert session.get(PatchCandidateRow, candidate_id).status == "IMPLEMENTED_CANDIDATE"

    fake_verification = client.post(
        "/api/v1/admin/platform/governance/verification-runs",
        json={
            "patch_candidate_id": candidate_id,
            "observed_candidate_sha": candidate_sha,
            "environment_identity": "isolated-test-worktree",
            "test_results": {
                "passed": 3,
                "failed": 0,
                "executed_test_ids": ["focused contract tests"],
                "evidence_classes": ["TEST_REPORT"],
            },
            "differential_results": {
                "baseline_failures": 2,
                "final_failures": 2,
                "new_failures": 0,
            },
            "rollback_evidence": ["application rollback retains additive schema"],
            "artifact_references": ["artifact:test-report"],
            "secret_scan_status": "PASS",
            "assertion_result_ids": ["assertion-result-1"],
            "invariant_result_ids": ["invariant-result-1"],
            "provenance": {"candidate_sha": candidate_sha},
        },
    )
    assert fake_verification.status_code == 409
    assert fake_verification.json()["detail"] == "ASSERTION_RESULT_MISSING_OR_CROSS_TENANT"

    assertion, invariant = _governance_evidence(session)
    verification = client.post(
        "/api/v1/admin/platform/governance/verification-runs",
        json={
            "patch_candidate_id": candidate_id,
            "observed_candidate_sha": candidate_sha,
            "environment_identity": "isolated-test-worktree",
            "test_results": {
                "passed": 3,
                "failed": 0,
                "executed_test_ids": ["focused contract tests"],
                "evidence_classes": ["TEST_REPORT"],
            },
            "differential_results": {
                "baseline_failures": 2,
                "final_failures": 2,
                "new_failures": 0,
            },
            "rollback_evidence": ["application rollback retains additive schema"],
            "artifact_references": ["artifact:test-report"],
            "secret_scan_status": "PASS",
            "assertion_result_ids": [assertion.id],
            "invariant_result_ids": [invariant.id],
            "provenance": {"candidate_sha": candidate_sha},
        },
    )
    assert verification.status_code == 201
    assert verification.json()["result"] == "PATCH_VERIFIED"

    blocking_finding = record_finding(
        session,
        FindingCandidate(
            tenant_id=DEFAULT_TENANT_ID,
            category=FindingCategory.BLOCKER,
            severity=FindingSeverity.LOW,
            title="Synthetic promotion blocker",
            summary="A current blocker must prevent promotion request creation.",
            component_key="platform.promotion",
            reason_code="SYNTHETIC_PROMOTION_BLOCKER",
        ),
    )
    session.commit()
    promotion_payload = {
        "patch_candidate_id": candidate_id,
        "verification_run_id": verification.json()["id"],
        "required_source_sha": candidate_sha,
        "required_schema_revision": "0019_platform_evolution_wave_d",
        "evidence_coverage": {"mandatory": "complete"},
        "blocking_finding_snapshot": [],
        "risk_disposition": {"RPE-023": "controlled"},
        "rollback_reference": "rollback:platform-test",
        "reason": "Candidate evidence is ready for a human decision.",
        "provenance": {"candidate_sha": candidate_sha},
    }
    stale_snapshot = client.post(
        "/api/v1/admin/platform/governance/promotion-requests",
        json=promotion_payload,
    )
    assert stale_snapshot.status_code == 409
    assert stale_snapshot.json()["detail"] == "BLOCKING_FINDING_SNAPSHOT_MISMATCH"

    current_snapshot = client.post(
        "/api/v1/admin/platform/governance/promotion-requests",
        json=promotion_payload
        | {"blocking_finding_snapshot": [blocking_finding.finding_id]},
    )
    assert current_snapshot.status_code == 409
    assert current_snapshot.json()["detail"] == "BLOCKING_FINDINGS_PRESENT"

    resolve_finding(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        finding_id=blocking_finding.finding_id,
        reason="Synthetic blocker closed by the test fixture.",
    )
    session.commit()
    promotion = client.post(
        "/api/v1/admin/platform/governance/promotion-requests",
        json=promotion_payload,
    )
    assert promotion.status_code == 201
    assert promotion.json()["decision"] == "PROMOTION_REQUIRED"

    self_asserted_approval = client.post(
        f"/api/v1/admin/platform/governance/promotion-requests/{promotion.json()['id']}/decisions",
        json={
            "decision": "APPROVED",
            "authority_type": "HUMAN_OPERATOR",
            "authority_reference": "caller-asserted",
            "reason": "Caller assertions are not a trusted authority channel.",
        },
    )
    assert self_asserted_approval.status_code == 422

    ai_approval = client.post(
        f"/api/v1/admin/platform/governance/promotion-requests/{promotion.json()['id']}/decisions",
        json={
            "decision": "APPROVED",
            "reason": "Automated approval must be denied.",
        },
    )
    assert ai_approval.status_code == 403
    assert ai_approval.json()["detail"] == "TRUSTED_HUMAN_PROMOTION_CHANNEL_REQUIRED"

    trusted_decision = record_human_promotion_decision(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        promotion_request_id=promotion.json()["id"],
        decision="APPROVED",
        authority_type="HUMAN_OPERATOR",
        authority_reference="verified-test-operator",
        reason="Server-side authority verifier accepted this test identity.",
        authority_verifier=lambda authority_type, reference: (
            authority_type == "HUMAN_OPERATOR" and reference == "verified-test-operator"
        ),
    )
    assert trusted_decision.decision == "APPROVED"
    with pytest.raises(GovernanceDenied, match="PROMOTION_REQUEST_ALREADY_DECIDED"):
        record_human_promotion_decision(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            promotion_request_id=promotion.json()["id"],
            decision="REJECTED",
            authority_type="HUMAN_OPERATOR",
            authority_reference="verified-test-operator",
            reason="A second terminal decision must be rejected.",
            authority_verifier=lambda _authority_type, _reference: True,
        )

    assert client.post(f"/api/v1/admin/platform/governance/diagnoses/{diagnosis_id}/apply").status_code == 404
    assert client.post(f"/api/v1/admin/platform/governance/remediation-proposals/{proposal_id}/apply").status_code == 404
    assert client.post(f"/api/v1/admin/platform/governance/patch-candidates/{candidate_id}/deploy").status_code == 404

    records = client.get("/api/v1/admin/platform/governance/records")
    assert records.status_code == 200
    assert len(records.json()["diagnoses"]) == 1
    assert len(records.json()["proposals"]) == 1
    assert len(records.json()["promotion_decisions"]) == 2


def test_promotion_state_uses_only_latest_request_terminal_decision():
    created_at = datetime(2026, 8, 22, tzinfo=UTC)
    first_request = SimpleNamespace(
        id="request-1",
        decision="PROMOTION_REQUIRED",
        promotion_request_id=None,
        provenance={},
        created_at=created_at,
    )
    first_approval = SimpleNamespace(
        id="decision-1",
        decision="APPROVED",
        promotion_request_id=first_request.id,
        provenance={},
        created_at=created_at + timedelta(seconds=1),
    )
    current_request = SimpleNamespace(
        id="request-2",
        decision="PROMOTION_REQUIRED",
        promotion_request_id=None,
        provenance={},
        created_at=created_at + timedelta(seconds=2),
    )
    assert promotion_state([first_request, first_approval, current_request]) == (
        "PROMOTION_REQUIRED"
    )

    current_rejection = SimpleNamespace(
        id="decision-2",
        decision="REJECTED",
        promotion_request_id=current_request.id,
        provenance={},
        created_at=created_at + timedelta(seconds=3),
    )
    decisions = [first_request, first_approval, current_request, current_rejection]
    assert promotion_state(decisions) == "REJECTED"
    with pytest.raises(GovernanceDenied, match="APPLICATION_WITHOUT_HUMAN_APPROVAL"):
        promotion_state(decisions, applied_evidence=True)

    conflicting_approval = SimpleNamespace(
        id="decision-3",
        decision="APPROVED",
        promotion_request_id=current_request.id,
        provenance={},
        created_at=created_at + timedelta(seconds=4),
    )
    with pytest.raises(GovernanceDenied, match="PROMOTION_REQUEST_TERMINAL_CONFLICT"):
        promotion_state(decisions + [conflicting_approval])
