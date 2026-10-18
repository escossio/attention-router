"""Proposal validation only; no DB, operator approval or production changes."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from attention_router.infrastructure.hashing import stable_hash
from scripts.forensics.andy_visitor_recovery_preflight import preflight_manifest, simulation_spec


PROPOSAL_PATH = Path(__file__).resolve().parents[1] / "config/examples/andy_visitor_recovery.proposed.json"


def proposal():
    return json.loads(PROPOSAL_PATH.read_text(encoding="utf-8"))


def test_preflight_binds_review_checksums_without_mutating_or_approving():
    manifest = proposal()
    before = deepcopy(manifest)
    report = preflight_manifest(manifest, tenant_approved=True, version_valid=True)
    active = simulation_spec(manifest)
    assert manifest == before
    assert report["proposal_valid"] is True
    assert report["manifest_sha256"] == stable_hash(manifest)
    assert report["activation_spec_sha256"] == stable_hash(active)
    assert report["policy_sha256"] == stable_hash(manifest["policy_proposal"])
    assert active["autonomy"]["level"] == "observe"
    assert active["autonomy"]["execution_mode"] == "AUTO_ALLOWED"
    assert manifest["creation_spec"]["autonomy"]["execution_mode"] == "OBSERVE"
    assert report["autonomy_increase_requires_approval"] is True
    assert report["scope_and_version_evidence"] == "CALLER_ASSERTIONS_ONLY"
    assert report["applies_changes"] is False
    assert report["operational_approval_granted"] is False
    assert report["production_recovery_proven"] is False
    assert report["selector_domain_or_publication_guard_installed"] is False
    assert report["runtime_requirements"]["AGENT_DECISION_DEFAULT_VISITOR_ONLY"] is True
    assert report["selection_impact"]["non_direct_and_groups_may_select_default"] is False


@pytest.mark.parametrize(
    "path,value,tenant_approved,version_valid,reason",
    [
        (("creation_spec", "domain"), "food_service", True, True, "DOMAIN_INVALID"),
        (("proposed_activation", "status"), "draft", True, True, "PUBLICATION_REQUIRED"),
        (("authority_boundary", "owner_authenticated"), True, True, True, "AUTHORITY_INVALID"),
        (("authority_boundary", "tools_enabled"), True, True, True, "AUTHORITY_INVALID"),
        (("policy_proposal", "allowed_capabilities"), ["gmail.read"], True, True, "POLICY_AUTHORITY_INVALID"),
        (("policy_proposal", "allowed_disclosures"), ["private_state"], True, True, "POLICY_AUTHORITY_INVALID"),
        ((), None, False, True, "TENANT_NOT_APPROVED"),
        ((), None, True, False, "VERSION_INVALID"),
        (("proposed_activation", "autonomy", "execution_mode"), "OBSERVE", True, True, "AUTONOMY_CHANGE_NOT_EXPLICIT"),
        (("proposed_activation", "autonomy", "execution_mode"), "REQUIRES_APPROVAL", True, True, "AUTONOMY_CHANGE_NOT_EXPLICIT"),
        (("selection_impact", "acknowledged_for_review"), False, True, True, "BLAST_RADIUS_NOT_ACKNOWLEDGED"),
        (("policy_proposal", "allowed_actions"), ["respond", "request_information", "grant.create"], True, True, "POLICY_AUTHORITY_INVALID"),
        (("runtime_requirements", "AGENT_DECISION_DEFAULT_VISITOR_ONLY"), False, True, True, "RUNTIME_GUARD_REQUIRED"),
        (("runtime_requirements", "private_platform_context_excluded"), False, True, True, "RUNTIME_GUARD_REQUIRED"),
        (("selection_impact", "owner_without_explicit_blueprint_may_select_default"), True, True, True, "BLAST_RADIUS_NOT_ACKNOWLEDGED"),
        (("creation_spec", "behavior"), {}, True, True, "PRIVATE_OR_IMPLICIT_CONTEXT_INVALID"),
    ],
    ids=[
        "wrong-domain", "unpublished", "owner-promotion", "tools",
        "private-capability", "private-disclosure", "tenant-not-approved",
        "invalid-version", "target-observe", "target-requires-approval",
        "unacknowledged-blast-radius", "legacy-policy-action-inheritance",
        "runtime-guard-disabled", "private-context-included", "owner-default-inheritance",
        "persistent-contract-missing",
    ],
)
def test_preflight_rejects_mutated_or_unproven_package(
    path, value, tenant_approved, version_valid, reason,
):
    manifest = proposal()
    target = manifest
    for key in path[:-1]:
        target = target[key]
    if path:
        target[path[-1]] = value
    with pytest.raises(ValueError, match=f"^VISITOR_RECOVERY_{reason}$"):
        preflight_manifest(manifest, tenant_approved=tenant_approved, version_valid=version_valid)


@pytest.mark.parametrize("field", ["tenant_approved", "version_valid"])
def test_scope_and_version_assertions_require_actual_booleans(field):
    evidence = {"tenant_approved": True, "version_valid": True, field: 1}
    with pytest.raises(ValueError, match="^VISITOR_RECOVERY_(TENANT_NOT_APPROVED|VERSION_INVALID)$"):
        preflight_manifest(proposal(), **evidence)
